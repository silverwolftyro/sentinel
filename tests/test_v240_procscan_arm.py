"""v2.4.0 回归：挂载会话的进程反查、守护者武装、意图锚定与雷达性能。

背景
====

v2.3.0 的雷达能**发现**用户正在跑的 Agent 日志并挂载进来，
但挂载进来的会话是**只读**的 —— 两个残缺：

1. **找不到 PID** —— 守护者需要明确的 PID 才能挂起进程；
2. **没有目标锚点** —— 旁路日志往往没有最初的 Prompt，
   没有锚点就做不了意图漂移（DRIFT）检测。

v2.4.0 把这两块补上，但**不扩大哨兵的动作边界**：

* 守护者只在用户**手动打开开关**之后才接管；
* 反查失败就**如实**显示「[未绑定进程]」，武装开关禁用。

本文件钉住：

1. **反查** —— 多策略、置信度、绝不认错人；
2. **武装** —— 开关的语义（武装 / 解除 / 未绑定时拒绝）；
3. **锚定** —— 手动设定目标后立刻启用滑窗机制（且结论能落地成告警）；
4. **性能** —— 雷达跳过已挂载文件；SSE 批量下发而不是逐行刷屏。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.core.engine import IntentTracker, SentinelEngine
from src.gui import server as gui_server
from src.gui.server import (
    AlertStore,
    EventHub,
    Watcher,
    create_app,
)
from src.procscan import (
    BINDABLE_CONFIDENCE,
    ProcessHit,
    detect_agent_in_cmdline,
    find_process,
    normalized_path,
    protected_pids,
    psutil_available,
)
from src.radar import scan_agent_sessions


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write_jsonl(path: str, lines) -> None:
    """写一份 JSONL 日志。"""
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _make_app(tmp_path, *, factory_kwargs=None, **kwargs):
    """造一个最小可用的看板应用（带一个假的挂载工厂）。

    :param tmp_path: pytest 的临时目录。
    :param factory_kwargs: 传给假工厂的额外字段
        （``task_goal`` / ``process_info`` / ``arm_callback``）。
    :param kwargs: 传给 :func:`create_app` 的参数。
    :return: ``(app, calls)``。
    """
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    calls = []
    extra = dict(factory_kwargs or {})

    def factory(info):
        """假工厂：只记录调用，不真的起引擎。"""
        calls.append(dict(info))
        built = {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": info.get("cwd", ""),
            "tail_runner": None,
        }
        built.update(extra)
        return built

    kwargs.setdefault("attach_factory", factory)
    app = create_app(
        str(watch), str(alert), start_watcher=False, **kwargs
    )
    return app, calls


# ---------------------------------------------------------------------------
# 任务一：进程反查
# ---------------------------------------------------------------------------
def test_自己永远在保护名单里():
    """哨兵自己**绝不能**被当成被监控进程。"""
    protected = protected_pids()
    assert os.getpid() in protected
    assert os.getppid() in protected


def test_置信度不够就不算绑定():
    """低于阈值的结果**不敢**拿去武装守护者。"""
    low = ProcessHit(pid=1, confidence=0.5)
    high = ProcessHit(pid=1, confidence=BINDABLE_CONFIDENCE)
    assert low.bindable is False
    assert high.bindable is True


def test_命令行能认出是哪个Agent():
    """从命令行里认出 Agent 类型。"""
    assert detect_agent_in_cmdline("codex --model gpt") == "codex"
    assert detect_agent_in_cmdline("mcode run") == "mcode"
    assert detect_agent_in_cmdline("claude -p") == "claude"
    assert detect_agent_in_cmdline("notepad.exe") == "unknown"


def test_路径归一化能忽略大小写差异():
    """Windows 上路径不区分大小写 —— 归一化后要能对上。"""
    assert normalized_path("") == ""
    assert normalized_path("C:/Temp") == os.path.normcase(
        os.path.abspath("C:/Temp")
    )


def test_查不到就是查不到():
    """查不到就返回 ``None`` —— 绝不拿「大概是某个进程」糊弄。"""
    hit = find_process(
        os.path.join(os.sep, "不存在的路径", "x.jsonl"), agent="codex"
    )
    assert hit is None


@pytest.mark.skipif(
    not psutil_available(), reason="psutil 不可用，跳过真实进程反查"
)
def test_命令行带日志路径时能反查到PID(tmp_path):
    """真实进程反查：命令行里带着日志路径 → 能认出来。"""
    log = tmp_path / "session.jsonl"
    log.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", str(log)]
    )
    try:
        time.sleep(1.0)
        hit = find_process(str(log))
        assert hit is not None
        assert hit.pid == proc.pid
        assert hit.strategy == "cmdline_path"
        assert hit.bindable is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.mark.skipif(
    not psutil_available(), reason="psutil 不可用，跳过真实进程反查"
)
def test_反查不会把自己认出来(tmp_path):
    """哨兵自己不能出现在结果里（否则会把自己挂起）。"""
    log = tmp_path / "self.jsonl"
    log.write_text("", encoding="utf-8")
    hits = gui_server._lookup_process(str(log))
    assert hits.get("pid") != os.getpid()


def test_反查失败时返回空字典(tmp_path):
    """反查器不可用 / 查不到 → 空字典（调用方据此显示「未绑定进程」）。"""
    missing = tmp_path / "nothing.jsonl"
    result = gui_server._lookup_process(str(missing))
    assert result == {}


# ---------------------------------------------------------------------------
# 任务一：武装守护者
# ---------------------------------------------------------------------------
class _FakeEngine:
    """假引擎：只关心 ``guardian`` 有没有被装上去。"""

    def __init__(self) -> None:
        self.guardian = None


class _FakeGuardian:
    """假守护者：只记录自己的目标 PID。"""

    def __init__(self, pid: int) -> None:
        self.target_pid = pid

    def status(self):
        return {"ok": True, "enabled": True, "target_pid": self.target_pid}


def test_武装后守护者才挂上(monkeypatch):
    """**只有**用户手动武装，守护者才接管那个 PID。"""
    monkeypatch.setattr(
        gui_server,
        "_build_guardian",
        lambda config_path, watch_path=None, target_pid=None: _FakeGuardian(
            int(target_pid or 0)
        ),
    )
    engine = _FakeEngine()
    assert engine.guardian is None

    result = gui_server._arm_attached_guardian(
        engine, None, pid=4321, armed=True, label="[Codex] D:\\work"
    )
    assert result["ok"] is True
    assert result["armed"] is True
    assert engine.guardian is not None
    assert engine.guardian.target_pid == 4321


def test_解除武装后守护者被摘掉(monkeypatch):
    """解除武装 → 恢复成纯只读监控。"""
    monkeypatch.setattr(
        gui_server,
        "_build_guardian",
        lambda config_path, watch_path=None, target_pid=None: _FakeGuardian(
            int(target_pid or 0)
        ),
    )
    engine = _FakeEngine()
    gui_server._arm_attached_guardian(
        engine, None, pid=4321, armed=True, label="x"
    )
    assert engine.guardian is not None

    result = gui_server._arm_attached_guardian(
        engine, None, pid=4321, armed=False, label="x"
    )
    assert result["ok"] is True
    assert result["armed"] is False
    assert engine.guardian is None


def test_没绑定进程时拒绝武装(monkeypatch):
    """没反查到 PID → **拒绝**武装，并说清楚原因。"""
    monkeypatch.setattr(
        gui_server,
        "_build_guardian",
        lambda *args, **kwargs: _FakeGuardian(1),
    )
    engine = _FakeEngine()
    result = gui_server._arm_attached_guardian(
        engine, None, pid=0, armed=True, label="x"
    )
    assert result["ok"] is False
    assert "未绑定进程" in result["error"]
    assert engine.guardian is None


def test_配置里守护者关闭时拒绝武装(monkeypatch):
    """守护者在配置里是关的 → 如实拒绝，不偷偷武装。"""
    monkeypatch.setattr(
        gui_server, "_build_guardian", lambda *args, **kwargs: None
    )
    engine = _FakeEngine()
    result = gui_server._arm_attached_guardian(
        engine, None, pid=4321, armed=True, label="x"
    )
    assert result["ok"] is False
    assert "守护者" in result["error"]
    assert engine.guardian is None


def test_武装端点会切换守护者(tmp_path):
    """``/api/sessions/{id}/arm`` 真的切换守护者的接管状态。"""
    state = {"armed": False, "guardian": None}

    def arm_callback(armed):
        """假武装回调：记录状态。"""
        state["armed"] = bool(armed)
        state["guardian"] = _FakeGuardian(4321) if armed else None
        return {
            "ok": True,
            "armed": bool(armed),
            "message": "ok",
            "_guardian": state["guardian"],
        }

    app, _calls = _make_app(
        tmp_path,
        factory_kwargs={
            "arm_callback": arm_callback,
            "process_info": {
                "pid": 4321, "bindable": True, "confidence": 0.95,
            },
        },
    )
    client = TestClient(app)

    target = tmp_path / "attached.jsonl"
    target.write_text("", encoding="utf-8")
    attached = client.post("/api/attach", json={"path": str(target)})
    assert attached.status_code == 200
    session_id = attached.json()["session"]["id"]

    # 反查成功 → 会话卡片显示 PID，武装开关可用。
    snapshot = attached.json()["session"]
    assert snapshot["attached"] is True
    assert snapshot["process_bound"] is True
    assert snapshot["pid"] == 4321
    assert snapshot["arm_available"] is True

    armed = client.post(
        "/api/sessions/{}/arm".format(session_id), json={"armed": True}
    )
    assert armed.status_code == 200
    assert armed.json()["ok"] is True
    assert state["armed"] is True

    disarmed = client.post(
        "/api/sessions/{}/arm".format(session_id), json={"armed": False}
    )
    assert disarmed.json()["ok"] is True
    assert state["armed"] is False


def test_反查失败时会话显示未绑定且不能武装(tmp_path):
    """反查失败 → PID 为 0、``process_bound`` 为假、武装不可用。"""
    app, _calls = _make_app(tmp_path)
    client = TestClient(app)

    target = tmp_path / "orphan.jsonl"
    target.write_text("", encoding="utf-8")
    attached = client.post("/api/attach", json={"path": str(target)})
    snapshot = attached.json()["session"]

    assert snapshot["attached"] is True
    assert snapshot["process_bound"] is False
    assert snapshot["pid"] == 0
    assert snapshot["arm_available"] is False

    armed = client.post(
        "/api/sessions/{}/arm".format(snapshot["id"]), json={"armed": True}
    )
    # 挂载工厂没给 arm_callback → 明确说「不支持武装」。
    assert armed.status_code == 400
    assert armed.json()["ok"] is False


# ---------------------------------------------------------------------------
# 任务二：挂载会话的意图锚定
# ---------------------------------------------------------------------------
def test_手动设定目标会立刻启用滑窗机制():
    """设定目标 → 引擎里**立刻**多出一个意图追踪器。"""

    class _Engine:
        """假引擎：记录追踪器换装。"""

        def __init__(self) -> None:
            self.intent_tracker = None
            self.installed = []

        def set_intent_tracker(self, tracker):
            """记录换装动作。"""
            self.installed.append(tracker)
            self.intent_tracker = tracker

    class _LLM:
        """假模型客户端。"""

        @staticmethod
        def evaluate_drift(goal, summary):
            return {"drift": False, "reason": "", "summary": ""}

    engine = _Engine()
    runner = gui_server.TailRunner(
        engine,
        "x.jsonl",
        task_goal=gui_server.TaskGoal(),
        drift_config={"window": 5, "threshold": 2, "enabled": True},
        llm_client=_LLM(),
    )
    assert engine.intent_tracker is None

    runner.apply_goal("重构 auth 模块并修复登录 bug")
    assert engine.intent_tracker is not None
    assert engine.intent_tracker.goal == "重构 auth 模块并修复登录 bug"
    assert len(engine.installed) == 1


def test_清空目标会停掉漂移检测():
    """清空目标 → 追踪器被摘掉（界面说「停用」就得真停用）。"""

    class _Engine:
        """假引擎。"""

        def __init__(self) -> None:
            self.intent_tracker = None

        def set_intent_tracker(self, tracker):
            """换装。"""
            self.intent_tracker = tracker

    class _LLM:
        """假模型客户端。"""

        @staticmethod
        def evaluate_drift(goal, summary):
            return {"drift": False, "reason": "", "summary": ""}

    engine = _Engine()
    runner = gui_server.TailRunner(
        engine,
        "x.jsonl",
        task_goal=gui_server.TaskGoal(),
        drift_config={"window": 5, "threshold": 2, "enabled": True},
        llm_client=_LLM(),
    )
    runner.apply_goal("修 bug")
    assert engine.intent_tracker is not None

    runner.apply_goal("")
    assert engine.intent_tracker is None


def test_换装追踪器会接上漂移结论回调():
    """运行中补建的追踪器**必须**接上回调，否则 DRIFT 告警发不出来。"""
    received = []

    class _Engine:
        """最小引擎：只需要一个回调落点。"""

        def __init__(self) -> None:
            self.intent_tracker = None
            # ⚠️ v3.5.0：``set_intent_tracker`` 现在还要清空
            #    **分路追踪器**（换装时旧的那批必须一起换掉），
            #    所以这个假引擎也得提供那两个属性。
            self._intent_trackers = {}
            self._tracker_lock = threading.Lock()
            # ⚠️ v4.0.0：``set_intent_tracker`` 还要把新追踪器接上
            #    **共享 L2 调度器**，所以这个假引擎也得提供该属性。
            #    给 ``None`` 即可 —— 语义是「退回直连判定回调」。
            self.l2_scheduler = None

        def _on_drift_result(self, verdict):
            """假回调。"""
            received.append(verdict)

        set_intent_tracker = SentinelEngine.set_intent_tracker

    engine = _Engine()
    tracker = IntentTracker(goal="修 bug", evaluator=lambda g, s: {})
    engine.set_intent_tracker(tracker)

    assert engine.intent_tracker is tracker
    assert tracker.async_enabled is True
    assert tracker.on_result == engine._on_drift_result


def test_挂载会话有自己的任务目标(tmp_path):
    """挂载会话的任务目标是**独立**的一份，互不干扰。"""
    goal_a = gui_server.TaskGoal()
    goal_b = gui_server.TaskGoal()
    goal_a.set("A 的目标")
    assert goal_b.get() == ""
    assert goal_a.get() == "A 的目标"


def test_设定会话目标会同步监控目标面板(tmp_path):
    """设定目标 → 「当前监控目标」面板要跟着变（不然像没生效）。"""
    app, _calls = _make_app(tmp_path)
    client = TestClient(app)

    target = tmp_path / "attached.jsonl"
    target.write_text("", encoding="utf-8")
    attached = client.post("/api/attach", json={"path": str(target)})
    session_id = attached.json()["session"]["id"]

    response = client.post(
        "/api/sessions/{}/task_goal".format(session_id),
        json={"goal": "重构 auth 模块"},
    )
    assert response.status_code == 200
    assert response.json()["goal"] == "重构 auth 模块"

    watch = client.get(
        "/api/sessions/{}/watch_target".format(session_id)
    )
    assert watch.json()["goal"] == "重构 auth 模块"
    assert watch.json()["goal_source"] == "explicit"


# ---------------------------------------------------------------------------
# 任务三：雷达跳过已挂载文件
# ---------------------------------------------------------------------------
def test_雷达跳过已挂载的文件(tmp_path):
    """已经被挂载的日志，雷达扫描时**不再读一遍**。"""
    root = tmp_path / ".codex"
    root.mkdir()
    kept = root / "kept.jsonl"
    skipped = root / "skipped.jsonl"
    _write_jsonl(str(kept), ['{"type":"turn.started"}'])
    _write_jsonl(str(skipped), ['{"type":"turn.started"}'])

    roots = [{"agent": "codex", "label": "Codex", "path": str(root)}]
    all_found = scan_agent_sessions(roots=roots)
    names = {os.path.basename(item.path) for item in all_found}
    assert "kept.jsonl" in names
    assert "skipped.jsonl" in names

    trimmed = scan_agent_sessions(roots=roots, exclude_paths=[str(skipped)])
    names = {os.path.basename(item.path) for item in trimmed}
    assert "kept.jsonl" in names
    assert "skipped.jsonl" not in names


def test_排除路径不受大小写影响(tmp_path):
    """Windows 上排除路径要忽略大小写。"""
    root = tmp_path / ".codex"
    root.mkdir()
    log = root / "Session.JSONL"
    _write_jsonl(str(log), ['{"type":"turn.started"}'])

    roots = [{"agent": "codex", "label": "Codex", "path": str(root)}]
    found = scan_agent_sessions(
        roots=roots,
        exclude_paths=[str(log).upper()],
    )
    assert found == []


# ---------------------------------------------------------------------------
# 任务三：SSE 微批处理
# ---------------------------------------------------------------------------
def _collect(hub: EventHub, timeout: float = 2.0):
    """把事件中心里的事件全捞出来（同步版本）。"""
    import asyncio

    async def scenario():
        queue = hub.subscribe()
        hub.attach_loop(asyncio.get_running_loop())
        return queue

    queue = asyncio.run(scenario())
    return queue


def test_日志行是批量下发而不是逐行刷屏(tmp_path):
    """一次写 300 行 → 发出去的**帧数**要远小于 300。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")

    hub = EventHub()
    store = AlertStore()
    watcher = Watcher(str(watch), str(alert), hub, store, poll_interval=0.05)

    published = []
    original = hub.publish

    def spy(event):
        """记录被发出去的事件。"""
        published.append(event)
        original(event)

    hub.publish = spy

    with open(str(watch), "a", encoding="utf-8") as handle:
        for index in range(300):
            handle.write("line {}\n".format(index))
        handle.flush()

    watcher._poll_once()

    line_events = [e for e in published if e.get("type") == "line"]
    batch_events = [e for e in published if e.get("type") == "lines"]
    assert line_events == []
    assert batch_events, "批量事件必须发出去"
    total = sum(len(e.get("texts") or []) for e in batch_events)
    assert total == 300
    # 300 行 → 帧数必须是「个位数级别」，而不是 300。
    assert len(batch_events) <= 5


def test_批量下发的行内容不丢(tmp_path):
    """批处理不能吞行 —— 一行都不能少。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")

    hub = EventHub()
    store = AlertStore()
    watcher = Watcher(str(watch), str(alert), hub, store, poll_interval=0.05)

    published = []
    hub.publish = published.append

    with open(str(watch), "a", encoding="utf-8") as handle:
        handle.write("第一行\n第二行\n第三行\n")
        handle.flush()

    watcher._poll_once()
    texts = []
    for event in published:
        if event.get("type") == "lines":
            texts.extend(event.get("texts") or [])
    assert texts == ["第一行", "第二行", "第三行"]
    assert store.recent_lines()[-3:] == ["第一行", "第二行", "第三行"]


def test_超大批次会被切块(tmp_path):
    """一批最多打包 ``line_batch_size`` 行 —— 不让单个事件无限膨胀。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")

    hub = EventHub()
    store = AlertStore()
    watcher = Watcher(
        str(watch),
        str(alert),
        hub,
        store,
        poll_interval=0.05,
        line_batch_size=50,
    )
    published = []
    hub.publish = published.append

    with open(str(watch), "a", encoding="utf-8") as handle:
        for index in range(120):
            handle.write("line {}\n".format(index))
        handle.flush()

    watcher._poll_once()
    batches = [e for e in published if e.get("type") == "lines"]
    assert len(batches) == 3
    assert all(len(e["texts"]) <= 50 for e in batches)
