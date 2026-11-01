"""一键复活测试（v2.1.0）。

覆盖：复活编排的四步顺序、只走留言箱的注入、
挂起进程只结束「哨兵自己挂起过的」、并发防呆、
以及「放行 ≠ 复活」这条设计前提。
"""

from __future__ import annotations

import importlib.util
import os
import threading

from src.core.injector import AgentInjector, InboxStore
from src.core.resurrect import RESURRECT_MESSAGE, ResurrectService
from src.core.safepoint import SafePointService


def _load_launcher():
    """加载启动器脚本（它不在 ``src`` 包里，是独立脚本）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        root, "_构建与发布", "scripts", "run_with_sentinel.py"
    )
    spec = importlib.util.spec_from_file_location("sentinel_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeGuardian:
    """假的守护者 —— 记录被调用了什么。"""

    def __init__(self, outcome=None):
        self.calls = []
        self._outcome = outcome or {"ok": True, "detail": "挂起的进程已结束"}

    def terminate_suspended(self, pid=None):
        self.calls.append(pid)
        return dict(self._outcome)

    def status(self):
        return {"enabled": True, "suspended_pids": [4242]}


class _FakeAgentControl:
    """假的进程控制 —— 记录被调用了什么。"""

    def __init__(self):
        self.relaunched = []
        self.state_calls = 0

    def state(self):
        self.state_calls += 1
        return {"ok": True, "pid": 4242, "alive": True, "suspended": True}

    def terminate(self):
        return {"ok": True}

    def relaunch(self, prompt=""):
        self.relaunched.append(prompt)
        return {"ok": True, "pid": 5555, "detail": "已重新拉起 Agent。"}


def _build(tmp_path, guardian=None, control=None):
    """造一套「工作目录 + 安全点服务 + 留言箱 + 复活服务」。"""
    work = tmp_path / "work"
    os.makedirs(work, exist_ok=True)
    with open(work / "app.py", "w", encoding="utf-8") as handle:
        handle.write("good\n")
    service = SafePointService(root=str(tmp_path / "points"))
    inbox = InboxStore(str(tmp_path / "sentinel_inbox.json"))
    agent_control = control if control is not None else _FakeAgentControl()
    resurrect = ResurrectService(
        safe_point_service=service,
        # ⚠️ 故意不给 stdin_writer —— 复活只走留言箱。
        injector=AgentInjector(inbox=inbox, stdin_writer=None),
        guardian=guardian if guardian is not None else _FakeGuardian(),
        agent_control=agent_control,
        label="s1",
    )
    return work, service, inbox, agent_control, resurrect


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def test_execute_rolls_back_then_injects_then_relaunches(tmp_path):
    """顺序必须是：回滚 → 注入 → 结束挂起进程 → 重拉。"""
    work, service, inbox, control, resurrect = _build(tmp_path)
    baseline = service.capture(str(work), label="基线安全点 · 会话起点")
    # Agent 把文件改坏了，又新建了一个。
    with open(work / "app.py", "w", encoding="utf-8") as handle:
        handle.write("broken\n")
    with open(work / "scratch.py", "w", encoding="utf-8") as handle:
        handle.write("junk\n")

    result = resurrect.execute("请改用安全的方式", {"alert_id": "a1"})

    assert result["ok"] is True
    # 回滚生效。
    assert (work / "app.py").read_text(encoding="utf-8") == "good\n"
    assert not (work / "scratch.py").exists()
    # 注入落进了留言箱。
    pending = inbox.pending()
    assert len(pending) == 1
    assert "请改用安全的方式" in pending[0].prompt
    # 进程被结束了。
    assert resurrect.guardian.calls == [None]
    # Agent 被重拉了。
    assert control.relaunched == ["请改用安全的方式"]
    assert result["new_pid"] == 5555
    assert result["message"] == RESURRECT_MESSAGE
    assert baseline.id


def test_execute_reports_fixed_message(tmp_path):
    """播报的那句话是**固定**的 —— 终端与看板看到的必须是同一句。"""
    _, _, _, _, resurrect = _build(tmp_path)

    result = resurrect.execute("提示")

    assert result["message"] == (
        "[SENTINEL] 已回滚至安全点，并注入纠偏提示，重新唤醒 Agent。"
    )


def test_injection_only_uses_inbox(tmp_path):
    """复活时**不能**往旧进程的 stdin 写 —— 它马上就要被结束了。

    真正的保证在启动器那边：它给复活服务造注入器时
    **故意不给 stdin_writer**。这里直接验那条契约。
    """
    launcher = _load_launcher()
    work = tmp_path / "work"
    os.makedirs(work, exist_ok=True)
    session = launcher.AgentSession.__new__(launcher.AgentSession)
    session.workdir = str(work)
    session.inbox = None
    session.plan = launcher.SessionPlan(
        id="s1", agent="mcode", task="干活", label="会话 1", cwd=str(work)
    )
    session.profile = launcher.AGENT_PROFILES["mcode"]
    session.safe_point_service = SafePointService(
        root=str(tmp_path / "points")
    )
    session.guardian = None

    service = session.build_resurrect_service()

    assert service.injector.stdin_writer is None
    assert service.injector.capabilities()["stdin"] is False

    # 注入确实只落进留言箱。
    result = service.injector.inject("提示")
    assert result.channel == "inbox"
    assert len(service.injector.inbox.pending()) == 1


def test_execute_without_agent_control_degrades(tmp_path):
    """没有进程控制能力时**不能**假装复活成功。"""
    *_, resurrect = _build(tmp_path)
    resurrect.agent_control = None

    result = resurrect.execute("提示")

    assert result["ok"] is False
    assert "无法重新拉起" in result["error"]


def test_execute_never_raises(tmp_path):
    """任何异常都只降级，绝不外泄（红线 R5）。"""
    _, _, _, _, resurrect = _build(tmp_path)

    class _Boom:
        def state(self):
            raise RuntimeError("炸了")

        def relaunch(self, prompt=""):
            raise RuntimeError("又炸了")

    resurrect.agent_control = _Boom()

    result = resurrect.execute("提示")

    assert result["ok"] is False
    assert "又炸了" in result["error"]


# ---------------------------------------------------------------------------
# 计划（只算不动）
# ---------------------------------------------------------------------------
def test_plan_lists_steps_without_touching_anything(tmp_path):
    """计划只算不动 —— 文件一个字节都不能改。"""
    work, service, _inbox, control, resurrect = _build(tmp_path)
    service.capture(str(work), label="基线安全点 · 会话起点")
    with open(work / "app.py", "w", encoding="utf-8") as handle:
        handle.write("broken\n")

    plan = resurrect.plan()

    assert plan["ok"] is True
    assert plan["restored_count"] == 1
    assert "app.py" in plan["restored"]
    # 文件还是坏的 —— 计划没有动手。
    assert (work / "app.py").read_text(encoding="utf-8") == "broken\n"
    assert control.relaunched == []
    assert any("回滚到安全点" in step for step in plan["steps"])


def test_plan_reports_agent_state(tmp_path):
    """计划里要带上 Agent 的状态（挂起没挂起）。"""
    _, _, _, _, resurrect = _build(tmp_path)

    plan = resurrect.plan()

    assert plan["agent"]["suspended"] is True
    assert plan["agent"]["pid"] == 4242


# ---------------------------------------------------------------------------
# 并发防呆
# ---------------------------------------------------------------------------
def test_concurrent_execute_is_rejected(tmp_path):
    """同一个会话连点两次 —— 第二个必须被挡住。"""
    _, _, _, control, resurrect = _build(tmp_path)  # noqa: RUF059
    started = threading.Event()
    release = threading.Event()

    class _SlowControl(_FakeAgentControl):
        def relaunch(self, prompt=""):
            started.set()
            release.wait(timeout=5)
            return super().relaunch(prompt)

    resurrect.agent_control = _SlowControl()

    first = {}
    thread = threading.Thread(
        target=lambda: first.update(resurrect.execute("提示"))
    )
    thread.start()
    assert started.wait(timeout=5)

    second = resurrect.execute("提示")

    release.set()
    thread.join(timeout=5)

    assert second["ok"] is False
    assert second.get("busy") is True
    assert "还在进行中" in second["error"]


def test_two_sessions_do_not_interfere(tmp_path):
    """两个会话各自独立 —— 各自回滚，互不影响。"""
    work_a = tmp_path / "a"
    work_b = tmp_path / "b"
    for path in (work_a, work_b):
        os.makedirs(path, exist_ok=True)
        with open(path / "app.py", "w", encoding="utf-8") as handle:
            handle.write("good\n")

    service_a = SafePointService(root=str(tmp_path / "pa"))
    service_b = SafePointService(root=str(tmp_path / "pb"))
    inbox_a = InboxStore(str(tmp_path / "inbox_a.json"))
    inbox_b = InboxStore(str(tmp_path / "inbox_b.json"))
    service_a.capture(str(work_a), label="基线安全点 · 会话起点")
    service_b.capture(str(work_b), label="基线安全点 · 会话起点")
    with open(work_a / "app.py", "w", encoding="utf-8") as handle:
        handle.write("broken-a\n")
    with open(work_b / "app.py", "w", encoding="utf-8") as handle:
        handle.write("broken-b\n")

    service_a_agent = ResurrectService(
        safe_point_service=service_a,
        injector=AgentInjector(inbox=inbox_a, stdin_writer=None),
        guardian=_FakeGuardian(),
        agent_control=_FakeAgentControl(),
        label="a",
    )
    service_b_agent = ResurrectService(
        safe_point_service=service_b,
        injector=AgentInjector(inbox=inbox_b, stdin_writer=None),
        guardian=_FakeGuardian(),
        agent_control=_FakeAgentControl(),
        label="b",
    )

    result_a = service_a_agent.execute("提示 A")
    result_b = service_b_agent.execute("提示 B")

    assert result_a["ok"] is True
    assert result_b["ok"] is True
    assert (work_a / "app.py").read_text(encoding="utf-8") == "good\n"
    assert (work_b / "app.py").read_text(encoding="utf-8") == "good\n"
    assert len(inbox_a.pending()) == 1
    assert len(inbox_b.pending()) == 1
    assert "提示 A" in inbox_a.pending()[0].prompt
    assert "提示 B" in inbox_b.pending()[0].prompt


# ---------------------------------------------------------------------------
# 守护者侧的作用域
# ---------------------------------------------------------------------------
def test_guardian_refuses_to_terminate_foreign_pid(tmp_path):
    """哨兵**没挂起过**的进程，守护者不碰。"""
    from src.core.guardian import Guardian

    guardian = Guardian({"guardian": {"enabled": False}})
    guardian._suspended_pids.add(1000)

    outcome = guardian.terminate_suspended(2000)

    assert outcome["ok"] is True
    assert "不是哨兵挂起的" in outcome["message"]


def test_guardian_refuses_protected_pid(tmp_path):
    """PID 0 / 1 / 哨兵自身 —— 一律拒绝。"""
    from src.core.guardian import Guardian

    guardian = Guardian({"guardian": {"enabled": False}})

    assert guardian.is_protected_pid(0) is True
    assert guardian.is_protected_pid(1) is True
    assert guardian.is_protected_pid(os.getpid()) is True
    assert guardian.is_protected_pid(os.getppid()) is True
