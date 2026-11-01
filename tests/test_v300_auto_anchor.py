"""v3.0.0 回归：全自动智能锚定与默认武装推荐。

背景
====

v2.4.0 把挂载会话从「只读」升级为「全功能」，
但用户还得**手动**做两件事：

1. 手动设目标（旁路日志往往没有最初的 Prompt）；
2. 手动开武装开关。

v3.0.0 要把这两件事做得**尽可能无感**：

* **自动提取意图** —— 哨兵自己从日志开头把用户最初那句
  Prompt 挖出来当锚点，界面上标「[自动提取]」；
* **默认武装推荐** —— PID 置信度 ≥ 0.95 时把开关**高亮**出来，
  并用气泡提示「高置信度匹配，建议开启守护者」。

⚠️ 两条铁律：
* **绝不自动武装** —— 挂起别人的进程必须用户自己点头（红线 R7）；
* **绝不假装** —— 提取不到就明说提取不到，不编一个目标出来。

本文件钉住：

1. 自动提取的**正确性**（三种 Agent 格式各一条）与**边界**；
2. 自动提取后追踪器**立刻**建起来（漂移监控真的在工作）；
3. 武装推荐**只高亮不武装**。
"""

from __future__ import annotations

import json
import os

import pytest

from src.gui import server as gui_server
from src.gui.server import TaskGoal, WatchTarget, create_app
from src.procscan import BINDABLE_CONFIDENCE


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write(path: str, payloads) -> None:
    """写一份 JSONL 日志。"""
    with open(path, "w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _make_app(tmp_path, *, factory_kwargs=None, **kwargs):
    """造一个最小可用的看板应用（带一个假的挂载工厂）。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    extra = dict(factory_kwargs or {})

    def factory(info):
        """假工厂：只记录调用，不真的起引擎。"""
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
    return create_app(
        str(watch), str(alert), start_watcher=False, **kwargs
    )


# ---------------------------------------------------------------------------
# 自动提取：三种 Agent 格式
# ---------------------------------------------------------------------------
def test_能从codex日志里提取出用户那句话(tmp_path):
    """Codex 格式：``item.completed`` + ``item.type == "user_message"``。"""
    log = tmp_path / "codex.jsonl"
    _write(log, [
        {"type": "thread.started", "thread_id": "t1"},
        {
            "type": "item.completed",
            "item": {
                "id": "i0",
                "type": "user_message",
                "content": "重构 auth 模块并修复登录 bug",
            },
        },
        {"type": "turn.started"},
    ])

    goal = gui_server._extract_initial_goal(str(log), "codex")

    assert goal == "重构 auth 模块并修复登录 bug"


def test_能从mcode日志里提取出用户那句话(tmp_path):
    """mcode 格式：扁平的 ``{"type": "user_message", ...}``。"""
    log = tmp_path / "mcode.jsonl"
    _write(log, [
        {"type": "user_message", "content": "把登录流程改成 OAuth"},
        {"type": "thinking", "text": "先看看现在怎么实现的"},
    ])

    goal = gui_server._extract_initial_goal(str(log), "mcode")

    assert goal == "把登录流程改成 OAuth"


def test_能从claude日志里提取出用户那句话(tmp_path):
    """Claude 格式：``{"type": "user", "message": {"content": "…"}}``。"""
    log = tmp_path / "claude.jsonl"
    _write(log, [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "user",
            "message": {"content": "修一下定时任务不触发的问题"},
        },
    ])

    goal = gui_server._extract_initial_goal(str(log), "claude")

    assert goal == "修一下定时任务不触发的问题"


def test_提取不到就是提取不到(tmp_path):
    """日志里没有用户消息 → 返回空串，**绝不编一个目标出来**。"""
    log = tmp_path / "nouser.jsonl"
    _write(log, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "i1", "type": "agent_message",
                     "text": "我已经改好了"},
        },
    ])

    assert gui_server._extract_initial_goal(str(log), "codex") == ""


def test_文件不存在时不报错(tmp_path):
    """读不到文件只降级，绝不抛异常挡住挂载。"""
    missing = tmp_path / "nothing.jsonl"

    assert gui_server._extract_initial_goal(str(missing), "codex") == ""


def test_提取的目标会被截断(tmp_path):
    """太长的目标要截断 —— 目标只是**锚点**，不是全文存档。"""
    log = tmp_path / "long.jsonl"
    _write(log, [
        {"type": "user_message", "content": "啊" * 5000},
    ])

    goal = gui_server._extract_initial_goal(str(log), "mcode")

    assert len(goal) == gui_server.INITIAL_GOAL_MAX_CHARS


def test_只扫开头不扫全文(tmp_path):
    """用户那句话一定在开头 —— 扫全文既慢又没必要。"""
    log = tmp_path / "late.jsonl"
    with open(log, "w", encoding="utf-8") as handle:
        for index in range(gui_server.INITIAL_GOAL_SCAN_LINES + 50):
            handle.write(
                json.dumps({"type": "thinking", "text": "x"}) + "\n"
            )
        handle.write(
            json.dumps({"type": "user_message", "content": "太晚了"}) + "\n"
        )

    assert gui_server._extract_initial_goal(str(log), "mcode") == ""


# ---------------------------------------------------------------------------
# 自动提取：接到挂载流程上
# ---------------------------------------------------------------------------
def test_挂载时自动设定目标并标成自动提取(tmp_path):
    """挂载 → 目标自动到位 → 面板标「自动提取」。"""
    from fastapi.testclient import TestClient

    log = tmp_path / "codex.jsonl"
    _write(log, [
        {"type": "thread.started", "thread_id": "t1"},
        {
            "type": "item.completed",
            "item": {
                "id": "i0",
                "type": "user_message",
                "content": "重构 auth 模块并修复登录 bug",
            },
        },
    ])

    goal = TaskGoal()
    captured = {}

    def factory(info):
        """假工厂：模拟真实工厂的行为（提取 → 设定目标）。"""
        extracted = gui_server._extract_initial_goal(
            info["path"], info.get("agent", "codex")
        )
        if extracted:
            goal.set(extracted)
        captured["goal"] = goal.get()
        captured["source"] = "auto" if extracted else "none"
        return {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": info.get("cwd", ""),
            "tail_runner": None,
            "task_goal": goal,
            "goal_source": captured["source"],
        }

    app = _make_app(tmp_path, attach_factory=factory)
    client = TestClient(app)

    response = client.post("/api/attach", json={"path": str(log)})
    snapshot = response.json()["session"]
    session_id = snapshot["id"]

    assert captured["goal"] == "重构 auth 模块并修复登录 bug"
    # 会话快照上的目标来源 = auto（= 界面上的「自动提取」）。
    assert snapshot["goal_source"] == "auto"

    target = client.get(
        "/api/sessions/{}/watch_target".format(session_id)
    ).json()
    assert target["goal"] == "重构 auth 模块并修复登录 bug"
    assert target["goal_source"] == "auto"
    assert target["goal_source_label"] == "自动提取"


def test_用户手动改目标会覆盖自动提取(tmp_path):
    """手动改过的目标**不许**被自动提取的结果盖回去。"""
    from fastapi.testclient import TestClient

    log = tmp_path / "codex.jsonl"
    _write(log, [
        {
            "type": "item.completed",
            "item": {"id": "i0", "type": "user_message",
                     "content": "自动提取的目标"},
        },
    ])

    goal = TaskGoal()

    def factory(info):
        """假工厂：自动设定目标。"""
        goal.set(gui_server._extract_initial_goal(info["path"], "codex"))
        return {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": "",
            "tail_runner": None,
            "task_goal": goal,
            "goal_source": "auto",
        }

    app = _make_app(tmp_path, attach_factory=factory)
    client = TestClient(app)

    attached = client.post("/api/attach", json={"path": str(log)})
    session_id = attached.json()["session"]["id"]

    client.post(
        "/api/sessions/{}/task_goal".format(session_id),
        json={"goal": "我手动改的目标"},
    )

    target = client.get(
        "/api/sessions/{}/watch_target".format(session_id)
    ).json()
    assert target["goal"] == "我手动改的目标"
    assert target["goal_source"] == "explicit"


def test_自动提取后追踪器立刻建起来():
    """自动提取的目标 → **立刻**建追踪器（漂移监控马上开工）。"""

    class _Engine:
        """假引擎。"""

        def __init__(self) -> None:
            self.intent_tracker = None
            self.installed = []

        def set_intent_tracker(self, tracker):
            """换装。"""
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
        task_goal=TaskGoal(),
        drift_config={"window": 5, "threshold": 2, "enabled": True},
        llm_client=_LLM(),
    )
    assert engine.intent_tracker is None

    # 模拟自动提取：设定目标 → 监听器触发 → 追踪器建起来。
    runner.engine.intent_tracker = None
    runner.apply_goal("重构 auth 模块并修复登录 bug")

    assert engine.intent_tracker is not None
    assert engine.intent_tracker.goal == "重构 auth 模块并修复登录 bug"


# ---------------------------------------------------------------------------
# 默认武装推荐
# ---------------------------------------------------------------------------
def test_高置信度门槛就是095():
    """推荐门槛固定在 0.95 —— 低于它的不敢建议用户开武装。"""
    assert BINDABLE_CONFIDENCE == 0.75
    assert gui_server.ARM_SUGGEST_CONFIDENCE == 0.95


def test_武装建议只是建议不会自动武装(tmp_path):
    """⚠️ 高置信度**绝不**自动武装 —— 必须用户自己点。"""
    from fastapi.testclient import TestClient

    armed_calls = []

    def arm_callback(armed):
        """假武装回调：记录有没有被自动调用过。"""
        armed_calls.append(armed)
        return {"ok": True, "armed": bool(armed), "message": "ok"}

    app = _make_app(
        tmp_path,
        factory_kwargs={
            "arm_callback": arm_callback,
            "process_info": {
                "pid": 4321,
                "bindable": True,
                "confidence": 0.99,
                "strategy": "cmdline_path",
            },
        },
    )
    client = TestClient(app)

    log = tmp_path / "attached.jsonl"
    log.write_text("", encoding="utf-8")
    attached = client.post("/api/attach", json={"path": str(log)})
    snapshot = attached.json()["session"]

    # 挂载之后**一次都没武装过**。
    assert armed_calls == []
    assert snapshot["armed"] is False
    assert snapshot["process_bound"] is True
    # 置信度摆在快照里，前端据此决定要不要高亮。
    assert snapshot["process_info"]["confidence"] == 0.99


def test_低置信度不进入可武装状态(tmp_path):
    """置信度不够 → 不绑定进程 → 武装开关**不可用**。"""
    from fastapi.testclient import TestClient

    app = _make_app(
        tmp_path,
        factory_kwargs={
            "arm_callback": lambda armed: {"ok": True, "armed": armed},
            "process_info": {
                "pid": 4321,
                "bindable": False,
                "confidence": 0.6,
                "strategy": "cwd_only",
            },
        },
    )
    client = TestClient(app)

    log = tmp_path / "attached.jsonl"
    log.write_text("", encoding="utf-8")
    attached = client.post("/api/attach", json={"path": str(log)})
    snapshot = attached.json()["session"]

    assert snapshot["process_bound"] is False
    assert snapshot["pid"] == 0
    assert snapshot["arm_available"] is False


# ---------------------------------------------------------------------------
# 目标来源标签
# ---------------------------------------------------------------------------
def test_目标来源标签齐全():
    """面板上的中文说明要齐全，不能出现裸的英文标识。"""
    labels = WatchTarget.GOAL_SOURCE_LABELS
    assert labels["auto"] == "自动提取"
    # v3.3.1：人工设定之后顶部显示的是「手动锚定」——
    # 说的是**结果**（漂移监控锚上了），不是「这个值是你手填的」。
    assert labels["explicit"] == "手动锚定"
    assert labels["config"] == "配置文件"
    assert labels["none"] == "未设置"


def test_自动锚定标记与来源一致():
    """``auto_anchored`` 只对「自动提取」为真。"""
    target = WatchTarget(agent="codex")
    target.set_goal("某目标", "auto")
    snapshot = target.snapshot()
    assert snapshot["auto_anchored"] is True
    assert snapshot["goal_source"] == "auto"

    target.set_goal("另一个目标", "explicit")
    assert target.snapshot()["auto_anchored"] is False


def test_临时目录里的文件不会留在仓库(tmp_path):
    """顺手确认一下：提取过程**只读**日志，不写任何东西。"""
    log = tmp_path / "codex.jsonl"
    _write(log, [
        {
            "type": "item.completed",
            "item": {"id": "i0", "type": "user_message", "content": "只读"},
        },
    ])
    before = os.path.getmtime(log)
    size = os.path.getsize(log)

    gui_server._extract_initial_goal(str(log), "codex")

    assert os.path.getmtime(log) == before
    assert os.path.getsize(log) == size


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
