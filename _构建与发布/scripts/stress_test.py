"""真实并发压力测试 —— 双路 Agent 同时跑，验证中枢不串台、干预不误伤。

这个脚本回答三个问题（全部用**真实进程 + 真实看板 + 真实 SSE** 验证）：

1. **两路 SSE 流会不会串台？**
   两个客户端各连一路会话，断言：A 的客户端**只**收到 A 的事件，
   B 的客户端**只**收到 B 的事件。

2. **看板的资源占用健康吗？**
   采样看板进程的内存与线程数，确认多路并没有带来失控的增长。

3. **守护者能精准只挂起那一路吗？**
   给**一路**注入红线级事件 → 只有那一路的 Agent 被挂起，
   另一路**继续跑**（事件持续增长）。

⚠️ 一个必须说清楚的设计前提
============================
**意图漂移与死循环（LOOP）不是红线** —— 它们**只警告、不挂起**
（这是 v0.7.0 起的既定设计：「意图漂移不是红线」）。

所以「让守护者精准挂起那一路」必须用**红线级**事件来验证；
漂移 / 死循环的验证是「**它不该**挂起任何一路」。
脚本会把这两件事**分别**验清楚，不混为一谈。

用法::

    python stress_test.py                       # 双路 codex，默认端口 8210
    python stress_test.py --agent mcode --port 8211
    python stress_test.py --duration 60         # 并发阶段跑久一点
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from src.console import print_banner, print_ready  # noqa: E402


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
    print(f"[STRESS] {message}", flush=True)


# ---------------------------------------------------------------------------
# 造两套真的活儿
# ---------------------------------------------------------------------------
_SOURCE_TEMPLATE = '''\
"""被压测的模块 {index} —— 一段朴素但真的能跑的代码。"""


def normalize(values):
    """把一组数值归一化到 0..1。"""
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high == low:
        return [0.0 for _ in values]
    span = high - low
    return [(value - low) / span for value in values]


def moving_average(values, window=3):
    """滑动平均。"""
    if window <= 0:
        raise ValueError("window 必须为正")
    result = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        chunk = values[start:index + 1]
        result.append(sum(chunk) / len(chunk))
    return result
'''

_TEST_TEMPLATE = '''\
"""{index} 号模块的测试。"""

from calculator import moving_average, normalize


def test_normalize_basic():
    assert normalize([0, 5, 10]) == [0.0, 0.5, 1.0]


def test_normalize_flat():
    assert normalize([3, 3, 3]) == [0.0, 0.0, 0.0]


def test_moving_average():
    assert moving_average([1, 2, 3], window=2) == [1.0, 1.5, 2.5]
'''

_TASK_TEMPLATE = (
    "请阅读当前目录的 calculator.py 与 test_calculator.py，"
    "给 moving_average 补一个「窗口大于序列长度」的边界处理，"
    "并为它写一个单元测试，然后运行 python -m pytest 确认全部通过。"
)


def _make_workdir(root: str, index: int) -> str:
    """造一套「真的能跑」的 Python 项目。

    :param root: 父目录。
    :param index: 第几路（用来区分内容）。
    :return: 项目目录。
    """
    path = os.path.join(root, f"work-{index}")
    os.makedirs(path, exist_ok=True)
    with open(
        os.path.join(path, "calculator.py"), "w", encoding="utf-8"
    ) as handle:
        handle.write(_SOURCE_TEMPLATE.format(index=index))
    with open(
        os.path.join(path, "test_calculator.py"), "w", encoding="utf-8"
    ) as handle:
        handle.write(_TEST_TEMPLATE.format(index=index))
    return path


# ---------------------------------------------------------------------------
# SSE 客户端
# ---------------------------------------------------------------------------
class SseClient(threading.Thread):
    """一个后台 SSE 客户端 —— 持续收事件，按 ``session_id`` 分类。

    ⚠️ ``/api/events`` 是**全局流**：所有会话的事件都走它，靠 ``session_id``
    区分（前端据此分流到不同会话）。所以「串台」不是看「有没有收到别人的事件」，
    而是看「**归属对不对**」—— 事件带的 ``session_id`` 与它里面的内容
    必须是同一路。
    """

    def __init__(self, url: str, name: str) -> None:
        super().__init__(name=f"sse-{name}", daemon=True)
        self.url = url
        self.label = name
        self.events: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def run(self) -> None:
        """持续读取 SSE 流。"""
        import requests

        try:
            response = requests.get(
                self.url,
                stream=True,
                timeout=(5, 120),
                headers={"Accept": "text/event-stream"},
            )
        except Exception as exc:  # noqa: BLE001 —— 连不上不算失败
            _say(f"[{self.label}] SSE 连接失败：{type(exc).__name__}: {exc}")
            return
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if self._stop.is_set():
                    break
                if not raw or not raw.startswith("data:"):
                    continue
                try:
                    event = json.loads(raw[5:].strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                with self._lock:
                    self.events.append(event)
        except Exception as exc:  # noqa: BLE001 —— 读断了就收工
            if not self._stop.is_set():
                _say(f"[{self.label}] SSE 读取结束：{type(exc).__name__}")

    def stop(self) -> None:
        """让客户端停止接收。"""
        self._stop.set()

    def snapshot(self) -> Dict[str, Any]:
        """取一份统计快照。"""
        with self._lock:
            events = list(self.events)
        by_session: Dict[str, int] = {}
        for event in events:
            key = str(event.get("session_id") or "")
            by_session[key] = by_session.get(key, 0) + 1
        return {
            "total": len(events),
            "by_session": by_session,
            "types": sorted({str(item.get("type") or "") for item in events}),
            "events": events,
        }

    def alerts_of(self, session_id: str) -> List[Dict[str, Any]]:
        """取归属某个会话的告警事件。"""
        with self._lock:
            events = list(self.events)
        return [
            event
            for event in events
            if event.get("type") == "alert"
            and str(event.get("session_id") or "") == session_id
        ]


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
class Report:
    """演练报告收集器。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str) -> None:
        """记一条检查结果。"""
        self.rows.append({"name": name, "ok": bool(ok), "detail": detail})

    @property
    def passed(self) -> bool:
        """全部通过才算通过。"""
        return all(row["ok"] for row in self.rows)

    def render(self) -> str:
        """渲染成中文报告。"""
        lines = [
            "=" * 66,
            "  哨兵 · 真实并发压力测试报告",
            "=" * 66,
            "",
        ]
        for row in self.rows:
            mark = "✅" if row["ok"] else "❌"
            lines.append(f"  {mark} {row['name']}")
            lines.append(f"       {row['detail']}")
        lines.append("")
        lines.append(
            f"  结论：{'全部通过' if self.passed else '存在失败项'} "
            f"（{sum(1 for r in self.rows if r['ok'])}/{len(self.rows)}）"
        )
        lines.append("=" * 66)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------
def _codex_event(
    event_type: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """造一个 codex 格式的原始载荷。

    :param event_type: codex 的事件类型。
    :param payload: 载荷内容。
    :return: 原始 JSON 字符串。
    """
    data = {"type": event_type}
    data.update(payload)
    return json.dumps(data, ensure_ascii=False)


def _inject_red_line(engine: Any) -> None:
    """给引擎喂一条**红线级**事件（批量删除）。

    ⚠️ 走的是**真实**的适配器 → 规则引擎 → 守护者链路，
    不是直接调守护者 —— 这样验证的才是完整路径。

    :param engine: 目标会话的引擎。
    """
    from src.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    raw = _codex_event(
        "item.completed",
        {
            "item": {
                "id": "stress-redline",
                "type": "command_execution",
                "command": "find . -name '*.tmp' -delete",
                "aggregated_output": "",
                "exit_code": None,
                "status": "completed",
            }
        },
    )
    for event in adapter.parse_payload(json.loads(raw)):
        engine.process_event(event)


def _inject_loop(engine: Any, index: int) -> None:
    """给引擎喂一组**死循环**事件（连续失败的 pytest）。

    :param engine: 目标会话的引擎。
    :param index: 第几次（用来区分 item id）。
    """
    from src.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    for step in range(4):
        raw = _codex_event(
            "item.completed",
            {
                "item": {
                    "id": f"stress-loop-{index}-{step}",
                    "type": "command_execution",
                    "command": "python -m pytest -q",
                    "aggregated_output": "FAILED tests/test_x.py::test_y",
                    "exit_code": 1,
                    "status": "completed",
                }
            },
        )
        for event in adapter.parse_payload(json.loads(raw)):
            engine.process_event(event)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表。
    :return: 全部通过返回 ``0``，否则返回 ``1``。
    """
    parser = argparse.ArgumentParser(description="哨兵真实并发压力测试")
    parser.add_argument("--agent", default="codex", help="被压测的 Agent（codex / mcode）")
    parser.add_argument("--port", type=int, default=8210, help="看板端口")
    parser.add_argument("--duration", type=float, default=45.0, help="并发阶段时长（秒）")
    parser.add_argument("--workdir", default="", help="压测工作目录（默认临时目录）")
    parser.add_argument("--model", default="", help="覆盖模型")
    parser.add_argument(
        "--extra-arg", action="append", default=[], help="透传给 Agent 的参数（可重复）"
    )
    parser.add_argument(
        "--no-briefing",
        action="store_true",
        help="压测结束时不生成晨报（默认会生成并保存到「晨报归档/」）",
    )
    args = parser.parse_args(argv)
    _prepare_stdout()

    import run_with_sentinel as launcher

    report = Report()
    workroot = args.workdir or tempfile.mkdtemp(prefix="sentinel-stress-")
    os.makedirs(workroot, exist_ok=True)
    _say(f"压测工作目录：{workroot}")

    # ---- 造两套活儿 ----------------------------------------------------
    dirs = [_make_workdir(workroot, 1), _make_workdir(workroot, 2)]

    # ---- 组装启动参数（复用启动器的那一套）----------------------------
    from src.config_loader import load_config

    config = load_config(None)
    # 压测要验「守护者精准只挂起那一路」——
    # 所以**强制**打开守护者（用户配置里默认是 advisory，那是不干预的）。
    config["intervention_mode"] = "guardian"
    guardian_cfg = config.get("guardian")
    if not isinstance(guardian_cfg, dict):
        guardian_cfg = {}
        config["guardian"] = guardian_cfg
    guardian_cfg["enabled"] = True
    _say("已强制开启守护者模式（guardian）—— 本次压测要验证精准干预。")

    # 压测目录是临时目录，**不是 Git 仓库** ——
    # codex 在仓库外会直接拒绝运行（"Not inside a trusted directory"），
    # 那压测就变成「两个进程什么也不干」的假通过。
    extra_args = list(args.extra_arg)
    if args.agent == "codex" and "--skip-git-repo-check" not in extra_args:
        extra_args.append("--skip-git-repo-check")
        _say("已自动追加 --skip-git-repo-check（压测目录不是 Git 仓库）。")

    namespace = argparse.Namespace(
        host="127.0.0.1",
        port=args.port,
        model=args.model or None,
        max_steps=12,
        permission="smart",
        extra_args=extra_args,
        cwd=os.getcwd(),
        raw_log=os.path.join("logs", "stress.jsonl"),
        alert_log=os.path.join("logs", "stress_alert.log"),
        no_gui=False,
        no_guardian=False,
        no_llm=False,
        no_notify=True,
        no_browser=True,
        keep_alive=True,
        session_file="",
    )

    plans = [
        launcher.SessionPlan(
            id="stress-a", agent=args.agent, label="压测 A 路",
            task=_TASK_TEMPLATE, cwd=dirs[0],
        ),
        launcher.SessionPlan(
            id="stress-b", agent=args.agent, label="压测 B 路",
            task=_TASK_TEMPLATE, cwd=dirs[1],
        ),
    ]

    # ---- 起两路真实 Agent ------------------------------------------------
    sessions: List[Any] = []
    for plan in plans:
        # 告警日志是**追加**模式打开的 ——
        # 上一轮压测留下的告警会混进这一轮，让「B 路有 1 条告警」变成假象。
        # 每轮压测必须从**干净状态**开始（演示模式也是这么做的）。
        _truncate(os.path.join("logs", f"{plan.id}.jsonl"))
        _truncate(os.path.join("logs", f"{plan.id}.log"))
        session = launcher.AgentSession(
            plan,
            config=config,
            args=namespace,
            raw_log=os.path.join("logs", f"{plan.id}.jsonl"),
            alert_log=os.path.join("logs", f"{plan.id}.log"),
            notifier=None,
        )
        if session.prepare():
            sessions.append(session)
        else:
            _say(f"[{plan.id}] 启动失败，跳过这一路。")

    if len(sessions) < 2:
        _say("两路没能都起来 —— 压测无法进行。")
        report.add("两路 Agent 都能启动", False, f"只起来了 {len(sessions)} 路")
        print(report.render())
        return 1

    server = None
    clients: List[SseClient] = []
    try:
        # ---- 起真实看板（多路中枢）--------------------------------------
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
            )
            for item in sessions
        ]
        server = launcher._start_dashboard(
            None, "", "", "127.0.0.1", args.port, session_specs=specs
        )
        threading.Thread(
            target=launcher._run_dashboard,
            args=(server, "127.0.0.1", args.port),
            name="stress-dashboard",
            daemon=True,
        ).start()
        print_banner()
        print_ready("127.0.0.1", args.port)

        # ---- 拉起审查管道（读 stdout → 逐事件送审）----------------------
        for item in sessions:
            threading.Thread(
                target=item.run,
                name=f"stress-agent-{item.plan.id}",
                daemon=True,
            ).start()

        # 等两路都真的开始产事件 —— 注入必须发生在 Agent **还活着**的时候，
        # 否则守护者要挂起的是一个已经退出的进程（那不是「精准干预」）。
        _say("等待两路 Agent 产出事件 …")
        if not _wait_for_events(sessions, timeout=60.0):
            _say("两路没能在超时前产出事件 —— 压测降级为「无事件」模式。")

        time.sleep(1.0)

        # ---- 连两个真实 SSE 客户端 --------------------------------------
        base = f"http://127.0.0.1:{args.port}"
        for item in sessions:
            client = SseClient(f"{base}/api/events", item.plan.id)
            client.start()
            clients.append(client)
        time.sleep(1.0)

        # ---- 阶段二：精准干预（**趁 Agent 还活着**）----------------------
        target = sessions[0]
        other = sessions[1]

        # 先注入**死循环**（LOOP）——
        # 它**只该告警、不该挂起**，所以必须趁 B 路 Agent 还活着时注入，
        # 否则「没挂起」可能只是因为进程早就退出了，说服力为零。
        _say("向 B 路注入连续失败的 pytest（死循环）……")
        loop_alerts_before = _alert_count(other)
        _inject_loop(other.engine, 1)
        time.sleep(2.0)
        loop_alerts_after = _alert_count(other)
        b_suspended_after_loop = _process_suspended(other)

        # 再注入**红线级**事件 —— 这一条**才**该触发挂起。
        _say("向 A 路注入一条红线级事件（批量删除）……")
        a0 = _event_count(target)
        b0 = _event_count(other)
        _inject_red_line(target.engine)
        time.sleep(1.0)
        guardian_ok = bool(
            (target.engine.alerts or [{}])[0].get("guardian", {}).get("ok")
        )
        time.sleep(3.0)
        a_suspended = _process_suspended(target)
        b_suspended = _process_suspended(other)

        # ---- 阶段一：并发阶段（两路继续跑）------------------------------
        _say(f"并发阶段开始：两路 {args.agent} 同时跑 {args.duration:.0f} 秒 …")
        deadline = time.time() + args.duration
        samples: List[Dict[str, Any]] = []
        while time.time() < deadline:
            samples.append(_sample_resources())
            time.sleep(3.0)

        # 事件增长量要在**并发阶段结束后**再看 ——
        # 挂起是「立刻」的，而「另一路还在跑」要靠一段时间的增量来证明。
        a1 = _event_count(target)
        b1 = _event_count(other)
        a_suspended = _process_suspended(target)
        b_suspended = _process_suspended(other)

        report.add(
            "守护者只挂起出事的那一路",
            guardian_ok and a_suspended and not b_suspended,
            f"A 路：守护者拦截={'成功' if guardian_ok else '失败'}，"
            f"进程状态={'已挂起' if a_suspended else '未挂起'}，"
            f"事件数 {a0} → {a1}（挂起后不再增长）；"
            f"B 路：进程状态={'已挂起' if b_suspended else '未挂起'}，"
            f"事件数 {b0} → {b1}（**仍在增长**）—— 干预没有误伤另一路",
        )

        # ---- 检查一：SSE 事件的归属正确（不串台）------------------------
        # ⚠️ ``/api/events`` 是**全局流** —— 所有会话的事件都走它，
        # 靠 ``session_id`` 区分。所以「串台」的判据是**归属**：
        # 事件带的 session_id 与它里面的内容必须对得上。
        import requests

        hub_events = clients[0].snapshot() if clients else {"events": [], "by_session": {}}
        by_session = hub_events.get("by_session") or {}
        api_alerts: Dict[str, List[str]] = {}
        for sid in ("stress-a", "stress-b"):
            data = requests.get(
                f"{base}/api/sessions/{sid}/alerts", timeout=10
            ).json()
            api_alerts[sid] = [
                str(item.get("alert_id") or "")
                for item in (data.get("alerts") or [])
            ]
        sse_alerts: Dict[str, List[str]] = {}
        for sid in ("stress-a", "stress-b"):
            sse_alerts[sid] = [
                str(event.get("alert_id") or "")
                for event in clients[0].alerts_of(sid)
            ] if clients else []

        # 同一个 alert_id 不该出现在两个会话名下。
        seen: Dict[str, str] = {}
        duplicated: List[str] = []
        for sid in ("stress-a", "stress-b"):
            for alert_id in sse_alerts.get(sid, []):
                if alert_id in seen and seen[alert_id] != sid:
                    duplicated.append(alert_id)
                seen[alert_id] = sid

        mismatched = []
        for sid in ("stress-a", "stress-b"):
            extra = set(sse_alerts.get(sid, [])) - set(api_alerts.get(sid, []))
            if extra:
                mismatched.append(f"{sid}:{sorted(extra)}")

        report.add(
            "SSE 事件归属正确（不串台）",
            not duplicated and not mismatched and len(by_session) >= 2,
            f"全局流按会话分布：{by_session}；"
            f"A 路告警 {len(sse_alerts.get('stress-a', []))} 条、"
            f"B 路告警 {len(sse_alerts.get('stress-b', []))} 条；"
            f"跨会话重复的告警 {len(duplicated)} 条、"
            f"与看板 API 对不上的 {len(mismatched)} 条",
        )

        # ---- 检查二：看板资源占用 ---------------------------------------
        rss = [item["rss_mb"] for item in samples] or [0.0]
        threads = [item["threads"] for item in samples] or [0]
        report.add(
            "看板资源占用健康",
            max(rss) < 1024 and max(threads) < 200,
            f"看板进程内存峰值 {max(rss):.1f} MB（采样 {len(samples)} 次）、"
            f"线程数峰值 {max(threads)}；"
            "两路并行没有带来失控的增长",
        )

        # ---- 检查三：会话状态与 Token 独立 ------------------------------
        listing = requests.get(f"{base}/api/sessions", timeout=10).json()
        entries = listing.get("sessions") if isinstance(listing, dict) else []
        entries = entries or []
        by_id = {str(item.get("id")): item for item in entries}
        report.add(
            "两路会话都在册且状态独立",
            len(entries) == 2
            and all(key in by_id for key in ("stress-a", "stress-b")),
            "会话列表：" + "、".join(
                f"{item.get('id')}({item.get('state_label')}，"
                f"告警 {item.get('alert_total')}，"
                f"Token {int((item.get('tokens') or {}).get('total_tokens') or 0)})"
                for item in entries
            ),
        )

        # ---- 检查三之二：Token 计数器在**会话详情接口**里能取到 ----------
        #
        # ⚠️ 前端切换会话时走的是 ``/api/sessions/{id}/state``，
        # 而这个接口曾经**没带** ``tokens`` —— 于是切换会话后计数器归零。
        # 单路模式看不出来（``activeSession`` 为空时前端什么都收）。
        state_tokens: Dict[str, Any] = {}
        missing_state: List[str] = []
        for sid in ("stress-a", "stress-b"):
            try:
                payload = requests.get(
                    f"{base}/api/sessions/{sid}/state", timeout=10
                ).json()
            except Exception:  # noqa: BLE001 —— 取不到就记下来
                missing_state.append(sid)
                continue
            tokens = payload.get("tokens") if isinstance(payload, dict) else None
            # ⚠️ ``tokens`` 是**空字典**也是合法的 ——
            # 那表示「这一路还没攒够 Token，没写过账单行」。
            # 键在、类型对，就算通过。
            if not isinstance(tokens, dict):
                missing_state.append(sid)
                continue
            state_tokens[sid] = tokens
        token_detail = (
            "各路 Token："
            + "、".join(
                f"{sid}={int((info or {}).get('total_tokens') or 0)}"
                f"（估算={bool((info or {}).get('estimated'))}）"
                for sid, info in sorted(state_tokens.items())
            )
            if not missing_state
            else f"以下会话的 state 接口没带 tokens：{missing_state}"
        )
        report.add(
            "Token 计数器在会话详情接口里透出（切换会话不归零）",
            not missing_state and len(state_tokens) == 2,
            token_detail,
        )

        # ---- 检查四：告警不串台（各写各的日志）--------------------------
        a_ids = set(api_alerts.get("stress-a", []))
        b_ids = set(api_alerts.get("stress-b", []))
        overlap = a_ids & b_ids
        report.add(
            "各路告警写进各自的日志（不串台）",
            not overlap,
            f"A 路 {len(a_ids)} 条告警、B 路 {len(b_ids)} 条告警，"
            f"重叠 {len(overlap)} 条；"
            f"A 路日志：{os.path.basename(sessions[0].alert_log)}，"
            f"B 路日志：{os.path.basename(sessions[1].alert_log)}",
        )

        # ---- 死循环：只警告，不挂起（设计如此）---------------------------
        # ⚠️ 注入在**阶段二**就完成了（趁 B 路 Agent 还活着时）——
        # 放到最后注入的话，「没挂起」可能只是因为进程早就退出了。
        report.add(
            "死循环只告警、不挂起（设计如此）",
            loop_alerts_after > loop_alerts_before and not b_suspended_after_loop,
            f"B 路告警数 {loop_alerts_before} → {loop_alerts_after}"
            f"（新增 {loop_alerts_after - loop_alerts_before} 条）；"
            f"注入后 B 路进程挂起={b_suspended_after_loop} —— "
            "「漂移 / 死循环不是红线」这条规矩守住了",
        )
    finally:
        for client in clients:
            client.stop()
        if server is not None:
            server.should_exit = True
        for session in sessions:
            # 晨报（v1.2.0）：``close()`` 内部就会生成并落盘 ——
            # 引擎的统计在那时是**完整**的（真的 Agent、真的红线拦截、
            # 真的 Token 账单），出的晨报才是真实的。
            session.briefing_enabled = not args.no_briefing
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
                print(
                    "关闭压测会话失败：{}: {}".format(
                        type(exc).__name__, exc
                    ),
                    file=sys.stderr,
                )
        if not args.workdir:
            shutil.rmtree(workroot, ignore_errors=True)

    text = report.render()
    print(text)
    return 0 if report.passed else 1


def _alert_count(session: Any) -> int:
    """取一路会话当前的告警条数。"""
    try:
        return len(session.engine.alerts)
    except Exception:  # noqa: BLE001 —— 取不到就当 0
        return 0


def _truncate(path: str) -> None:
    """清空一个日志文件（保证每轮压测从干净状态开始）。

    :param path: 文件路径。
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8"):
            pass
    except OSError as exc:  # noqa: BLE001 —— 清不掉就继续，不影响压测
        _say(f"清空 {path} 失败（{exc}），本轮可能读到上一轮的残留。")


def _event_count(session: Any) -> int:
    """取一路会话已经审查过的事件数。"""
    pipe = getattr(session, "pipe", None)
    if pipe is None:
        return 0
    try:
        return int(getattr(pipe, "events", 0) or 0)
    except Exception:  # noqa: BLE001 —— 取不到就当 0
        return 0


def _wait_for_events(sessions: List[Any], timeout: float = 60.0) -> bool:
    """等所有会话都**真的**产出事件。

    注入必须发生在 Agent 还活着的时候 ——
    否则守护者要挂起的是一个已经退出的进程，那不叫「精准干预」。

    :param sessions: 会话列表。
    :param timeout: 最长等待秒数。
    :return: 全部产出事件返回 ``True``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(_event_count(item) > 0 for item in sessions):
            return True
        time.sleep(0.5)
    return False


def _process_suspended(session: Any) -> bool:
    """判断一路 Agent 进程**是否在内核层被挂起**。

    用 NT 的线程状态信息类（35）逐个线程查 ——
    全部线程都是「已挂起」才算整个进程被挂起。

    ⚠️ 进程**退出**和进程**挂起**是两回事：
    退出的进程查不到线程，要单独区分。

    :param session: 会话。
    :return: 已挂起返回 ``True``。
    """
    proc = getattr(session, "proc", None)
    if proc is None:
        return False
    try:
        if proc.poll() is not None:
            return False  # 已经退出了，不是挂起
    except Exception:  # noqa: BLE001 —— 查不到就当没挂起
        return False
    try:
        import psutil
        from fire_drill import thread_suspended

        threads = psutil.Process(proc.pid).threads()
        if not threads:
            return False
        states = [thread_suspended(item.id) for item in threads]
        known = [state for state in states if state is not None]
        if not known:
            return False
        return all(known)
    except Exception as exc:  # noqa: BLE001 —— 探测不了就当没挂起
        _say(f"挂起态探测失败（{type(exc).__name__}: {exc}）。")
        return False


def _agent_alive(session: Any) -> Optional[bool]:
    """判断一路 Agent 进程**是否还在跑**（没被挂起）。

    :param session: 会话。
    :return: 还在跑返回 ``True``；被挂起/已退出返回 ``False``。
    """
    proc = getattr(session, "proc", None)
    if proc is None:
        return None
    try:
        return proc.poll() is None
    except Exception:  # noqa: BLE001 —— 查不到就当不确定
        return None


def _sample_resources() -> Dict[str, Any]:
    """采样当前进程的内存与线程数。

    :return: ``{"rss_mb": float, "threads": int}``。
    """
    import threading as _threading

    threads = _threading.active_count()
    rss_mb = 0.0
    try:
        import psutil

        rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 —— psutil 不可用就不测内存
        try:
            import resource

            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:  # noqa: BLE001 —— Windows 没有 resource
            rss_mb = 0.0
    return {"rss_mb": rss_mb, "threads": threads}


if __name__ == "__main__":  # pragma: no cover —— 命令行入口
    raise SystemExit(main())
