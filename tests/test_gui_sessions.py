"""哨兵 GUI **多路会话中枢**测试（v1.0.0）。

验证四件事：

1. ``/api/sessions`` 能列出全部会话及其状态；
2. 会话级接口（告警 / 目标 / 暂停 / 守护者）**各管各的**，
   不会串台 —— 这是多路中枢最容易出的错；
3. SSE 事件带 ``session_id`` —— 前端据此分流；
4. v0.x 的单会话用法**完全不受影响**（老接口还在、老行为不变）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from src.gui.server import (
    SessionSpec,
    WatchTarget,
    create_app,
)


class _FakeGuardian:
    """守护者替身 —— 记录自己被谁调用过。"""

    def __init__(self, name: str = "g") -> None:
        self.name = name
        self.calls: List[str] = []
        self.overridden: Dict[str, Any] = {}

    def status(self) -> Dict[str, Any]:
        """返回守护者状态。"""
        self.calls.append("status")
        return {
            "ok": True,
            "enabled": True,
            "available": True,
            "name": self.name,
        }

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次干预。"""
        self.calls.append("intervene")
        return {"ok": True, "method": "suspend", "pid": 1234}

    def override(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次放行。"""
        self.calls.append("override")
        self.overridden = dict(payload)
        return {"ok": True, "resumed": True, "rule_id": payload.get("rule_id")}


def _write_alert(path: Path, alert_id: str, rule: str) -> None:
    """往告警日志里写一条机器行告警。"""
    payload = {
        "alert_id": alert_id,
        "timestamp": 1793070000.5,
        "verdict": "DANGEROUS",
        "level": "critical",
        "rule_id": rule,
        "reason": "红线级命中",
        "command": "rm -rf /tmp/x",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            "2026-10-27 10:00:00,000 WARNING ALERT_JSON "
            + json.dumps(payload, ensure_ascii=False)
            + "\n"
        )


def _make_multi_app(tmp_path):
    """造一个含三个会话的应用。"""
    specs = []
    for name in ("codex", "mcode", "claude"):
        watch = tmp_path / f"{name}.jsonl"
        watch.write_text("", encoding="utf-8")
        alert_log = tmp_path / f"{name}_alerts.log"
        alert_log.write_text("", encoding="utf-8")
        specs.append(
            SessionSpec(
                id=name,
                label=f"{name} 会话",
                agent=name,
                watch_path=str(watch),
                alert_log_path=str(alert_log),
                guardian_callback=_FakeGuardian(name),
                watch_target=WatchTarget(agent=name),
            )
        )
    return create_app(sessions=specs, start_watcher=False)


# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------
class TestSessionList:
    """/api/sessions —— 全局总览。"""

    def test_lists_all_sessions(self, tmp_path):
        """三个会话都要列出来。"""
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            data = client.get("/api/sessions").json()

        assert data["ok"] is True
        ids = [item["id"] for item in data["sessions"]]
        assert ids == ["codex", "mcode", "claude"]

    def test_session_snapshot_has_required_fields(self, tmp_path):
        """每个会话的快照必须带齐「全局状态」要显示的字段。"""
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            data = client.get("/api/sessions").json()

        item = data["sessions"][0]
        for key in (
            "id",
            "label",
            "agent",
            "state",
            "state_label",
            "counts",
            "alert_total",
            "goal",
            "goal_source",
            "goal_source_label",
        ):
            assert key in item, f"缺少字段 {key}"

    def test_state_label_is_human_readable(self, tmp_path):
        """状态标签要是中文人话，不是原始枚举值。"""
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            data = client.get("/api/sessions").json()

        assert data["sessions"][0]["state_label"] in (
            "启动中",
            "运行中",
            "已挂起",
            "空闲",
            "已结束",
        )


# ---------------------------------------------------------------------------
# 会话级接口：各管各的
# ---------------------------------------------------------------------------
class TestSessionIsolation:
    """多路中枢最容易出的错是「串台」—— 必须各管各的。"""

    def test_alerts_are_isolated_per_session(self, tmp_path):
        """A 会话只能看到 A 的告警。"""
        app = _make_multi_app(tmp_path)
        _write_alert(tmp_path / "codex_alerts.log", "a-1", "rule_a")
        _write_alert(tmp_path / "mcode_alerts.log", "b-1", "rule_b")
        app.state.sessions.get("codex").store.add_alert(
            {"alert_id": "a-1", "verdict": "DANGEROUS", "rule_id": "rule_a"}
        )
        app.state.sessions.get("mcode").store.add_alert(
            {"alert_id": "b-1", "verdict": "DANGEROUS", "rule_id": "rule_b"}
        )
        with TestClient(app) as client:
            codex = client.get("/api/sessions/codex/alerts").json()
            mcode = client.get("/api/sessions/mcode/alerts").json()

        assert [item["alert_id"] for item in codex["alerts"]] == ["a-1"]
        assert [item["alert_id"] for item in mcode["alerts"]] == ["b-1"]

    def test_watch_target_is_isolated_per_session(self, tmp_path):
        """每个会话的目标各是各的。"""
        app = _make_multi_app(tmp_path)
        app.state.sessions.get("codex").watch_target.set_goal(
            "写个 hello", source="auto"
        )
        with TestClient(app) as client:
            codex = client.get("/api/sessions/codex/watch_target").json()
            mcode = client.get("/api/sessions/mcode/watch_target").json()

        assert codex["goal"] == "写个 hello"
        assert codex["goal_source"] == "auto"
        assert mcode["goal"] == ""

    def test_pause_is_isolated_per_session(self, tmp_path):
        """暂停 A 不能影响 B。"""
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            client.post("/api/sessions/codex/pause", json={"paused": True})
            codex = client.get("/api/sessions/codex/state").json()
            mcode = client.get("/api/sessions/mcode/state").json()

        assert codex["paused"] is True
        assert mcode["paused"] is False

    def test_guardian_override_routes_to_right_session(self, tmp_path):
        """★ 放行必须打到**这条告警所属会话**的守护者去。

        放行 A 的进程却解除了 B 的挂起 —— 那就乱套了。
        """
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            response = client.post(
                "/api/sessions/mcode/guardian/override",
                json={"rule_id": "rule_b", "source": "gui"},
            )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        mcode_guardian = app.state.sessions.get("mcode").guardian
        codex_guardian = app.state.sessions.get("codex").guardian
        assert "override" in mcode_guardian.calls
        assert "override" not in codex_guardian.calls
        assert mcode_guardian.overridden["rule_id"] == "rule_b"

    def test_guardian_status_routes_to_right_session(self, tmp_path):
        """守护者状态也要按会话取。"""
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            codex = client.get("/api/sessions/codex/guardian/status").json()

        assert codex["name"] == "codex"

    def test_unknown_session_returns_404(self, tmp_path):
        """不存在的会话要明确报 404，而不是悄悄返回别的会话。"""
        app = _make_multi_app(tmp_path)
        with TestClient(app) as client:
            response = client.get("/api/sessions/nope/alerts")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# SSE 事件分流
# ---------------------------------------------------------------------------
class TestSessionEventRouting:
    """SSE 事件必须带 session_id —— 前端据此分流。"""

    def test_events_carry_session_id(self, tmp_path):
        """推给前端的事件带会话 ID。"""
        app = _make_multi_app(tmp_path)
        hub = app.state.hub
        import asyncio

        async def _read():
            queue = hub.subscribe()
            hub.attach_loop(asyncio.get_running_loop())
            # 必须**先订阅再发布** —— 反过来的话事件没人接。
            hub.publish(
                {"type": "alert", "session_id": "codex", "verdict": "X"}
            )
            event = await asyncio.wait_for(queue.get(), timeout=2.0)
            hub.unsubscribe(queue)
            return event

        event = asyncio.run(_read())
        assert event["session_id"] == "codex"

    def test_watcher_stamps_session_id_on_events(self, tmp_path):
        """★ 跟随线程推出去的事件必须自带会话 ID。

        这是多路分流的地基 —— 没有它，前端分不清告警来自哪个 Agent。
        """
        app = _make_multi_app(tmp_path)
        session = app.state.sessions.get("mcode")
        stamped = session.watcher._event({"type": "alert", "verdict": "X"})
        assert stamped["session_id"] == "mcode"

    def test_events_of_different_sessions_are_distinguishable(
        self, tmp_path
    ):
        """两个会话的事件不会混成同一个 ID。"""
        app = _make_multi_app(tmp_path)
        codex = app.state.sessions.get("codex").watcher._event({"type": "x"})
        mcode = app.state.sessions.get("mcode").watcher._event({"type": "x"})
        assert codex["session_id"] != mcode["session_id"]


# ---------------------------------------------------------------------------
# 兼容性：v0.x 单会话用法不变
# ---------------------------------------------------------------------------
class TestSingleSessionCompatibility:
    """v0.x 的单会话用法必须**完全不受影响**。"""

    def test_legacy_endpoints_still_work(self, tmp_path):
        """老接口还在，行为不变。"""
        watch = tmp_path / "agent.jsonl"
        watch.write_text("", encoding="utf-8")
        alert_log = tmp_path / "alerts.log"
        alert_log.write_text("", encoding="utf-8")
        app = create_app(str(watch), str(alert_log), start_watcher=False)
        with TestClient(app) as client:
            assert client.get("/api/state").status_code == 200
            assert client.get("/api/alerts").status_code == 200
            assert client.get("/api/watch_target").status_code == 200
            assert client.get("/api/task_goal").status_code == 200
            assert client.get("/api/guardian/status").status_code == 200
            sessions = client.get("/api/sessions").json()

        assert len(sessions["sessions"]) == 1
        assert sessions["sessions"][0]["id"] == "default"

    def test_legacy_scan_still_uses_primary_session(self, tmp_path):
        """老扫描回调仍作用于主会话的被监控文件。"""
        watch = tmp_path / "agent.jsonl"
        watch.write_text("", encoding="utf-8")
        alert_log = tmp_path / "alerts.log"
        alert_log.write_text("", encoding="utf-8")
        seen: List[str] = []

        def _scan(target: str) -> Dict[str, Any]:
            seen.append(target)
            return {"counts": {"ON_TRACK": 3}}

        app = create_app(
            str(watch),
            str(alert_log),
            scan_callback=_scan,
            start_watcher=False,
        )
        with TestClient(app) as client:
            response = client.post("/api/scan")

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert seen and seen[0].endswith("agent.jsonl")


# ---------------------------------------------------------------------------
# 会话模型
# ---------------------------------------------------------------------------
class TestSessionModel:
    """Session / SessionRegistry 的行为。"""

    def test_registry_get_falls_back_to_primary(self, tmp_path):
        """找不到 ID 时退回主会话 —— 单会话老代码不用改。"""
        app = _make_multi_app(tmp_path)
        registry = app.state.sessions
        assert registry.get("不存在的").id == "codex"
        assert registry.get(None).id == "codex"

    def test_registry_snapshot_order(self, tmp_path):
        """会话按登记顺序列出。"""
        app = _make_multi_app(tmp_path)
        ids = [item["id"] for item in app.state.sessions.snapshot()]
        assert ids == ["codex", "mcode", "claude"]

    def test_session_state_is_idle_when_not_started(self, tmp_path):
        """没启动跟随线程时状态是「空闲」。"""
        app = _make_multi_app(tmp_path)
        session = app.state.sessions.get("codex")
        assert session.state() == "idle"

    def test_session_state_paused_after_pause(self, tmp_path):
        """暂停后状态变成「已挂起」。"""
        app = _make_multi_app(tmp_path)
        session = app.state.sessions.get("codex")
        session.store.set_paused(True)
        assert session.state() == "paused"

    def test_removed_session_disappears(self, tmp_path):
        """移除会话后列表里不再有它。"""
        app = _make_multi_app(tmp_path)
        registry = app.state.sessions
        registry.remove("mcode")
        ids = [item["id"] for item in registry.snapshot()]
        assert "mcode" not in ids
