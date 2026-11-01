"""守护者（Guardian）测试。

**测试永远不该真的挂起谁的进程** —— 所以这里的进程控制器全部是
假实现，只记录调用。唯一会去碰 ``os.kill`` 的地方也用 mock 拦下来，
并断言那条路径**永远不会被走到**。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

import pytest

from src.core import guardian as guardian_module
from src.core.guardian import (
    INTERCEPT_MESSAGE,
    Guardian,
    ProcessController,
)
from src.rules.basic import SEVERITY_RED_LINE, VERDICT_DANGEROUS, RuleEngine


# ---------------------------------------------------------------------------
# 假进程控制器
# ---------------------------------------------------------------------------
class FakeController:
    """假进程控制器 —— 只记录调用，不真的动任何进程。

    :param ok: 模拟动作是否成功。
    """

    def __init__(self, ok: bool = True) -> None:
        self.calls: List[Tuple[str, int, str]] = []
        self.ok = ok
        self.prefer = "auto"

    def _record(self, action: str, pid: int, action_arg: str = "") -> Dict[str, Any]:
        self.calls.append((action, int(pid), action_arg))
        return {
            "ok": self.ok,
            "action": action,
            "pid": int(pid),
            "detail": f"[fake] {action}",
            "error": "" if self.ok else "[fake] 失败",
        }

    def suspend(self, pid: int) -> Dict[str, Any]:
        """假挂起。"""
        return self._record("suspend", pid)

    def resume(self, pid: int) -> Dict[str, Any]:
        """假恢复。"""
        return self._record("resume", pid)

    def interrupt(self, pid: int) -> Dict[str, Any]:
        """假中断。"""
        return self._record("sigint", pid)

    def intervene(self, pid: int, action: str = "") -> Dict[str, Any]:
        """假干预。"""
        return self._record("intervene", pid, action)


def _config(tmp_path: Any, **overrides: Any) -> Dict[str, Any]:
    """构造一份带 guardian 段的配置。"""
    guardian_config = {
        "enabled": True,
        "target_pid": 4242,
        "action": "auto",
        "audit_log": str(tmp_path / "guardian.log"),
        "override_log": str(tmp_path / "guardian_overrides.json"),
    }
    guardian_config.update(overrides.pop("guardian", {}))
    config = {
        "alert": {"intervention_mode": "guardian"},
        "guardian": guardian_config,
    }
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# 拦截
# ---------------------------------------------------------------------------
class TestIntervene:
    """红线级事件必须被拦截。"""

    def test_should_intervene_red_line_rule(self, tmp_path: Any) -> None:
        """红线规则命中后应调用进程控制器。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        result = guardian.intervene(
            {"rule_id": "catastrophic_root_delete", "reason": "红线"}
        )
        assert result["ok"] is True
        assert controller.calls, "进程控制器一次都没被调用"
        assert controller.calls[0][0] == "intervene"
        assert controller.calls[0][1] == 4242

    def test_should_not_intervene_when_disabled(self, tmp_path: Any) -> None:
        """守护者关闭时不得动手。"""
        controller = FakeController()
        config = _config(tmp_path)
        config["alert"]["intervention_mode"] = "advisory"
        config["guardian"]["enabled"] = False
        guardian = Guardian(config, controller=controller)
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})
        assert result["ok"] is False
        assert controller.calls == []

    def test_should_emit_intercept_message(self, tmp_path: Any) -> None:
        """拦截提示必须是用户认得的那句话。

        v0.6.0 起，这条提示**随告警一起输出**（由引擎的
        ``_emit_alert`` 负责），不再单独发 ``on_event`` ——
        两处都发会让用户在终端看到同一句话两遍。
        """
        events: List[Dict[str, Any]] = []
        guardian = Guardian(
            _config(tmp_path),
            controller=FakeController(),
            on_event=events.append,
        )
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})
        assert result["message"] == INTERCEPT_MESSAGE
        assert "override" in INTERCEPT_MESSAGE
        assert events == [], "拦截提示不应重复发事件（会与告警行重复）"

    def test_should_refuse_missing_rule_id(self, tmp_path: Any) -> None:
        """没有 rule_id 时不得动手。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        result = guardian.intervene({})
        assert result["ok"] is False
        assert controller.calls == []

    def test_should_refuse_protected_pid(self, tmp_path: Any) -> None:
        """PID 0 / 1 / 自身 / 父进程一律拒绝干预。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        for pid in (0, 1, os.getpid(), os.getppid()):
            result = guardian.intervene(
                {"rule_id": "catastrophic_root_delete", "pid": pid}
            )
            assert result["ok"] is False, f"PID {pid} 不该被干预"
        assert controller.calls == []

    def test_should_report_when_pid_unknown(self, tmp_path: Any) -> None:
        """定位不到 PID 时如实报告，而不是乱猜一个。"""
        controller = FakeController()
        config = _config(tmp_path, guardian={"target_pid": None})
        guardian = Guardian(config, controller=controller)
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})
        assert result["ok"] is False
        assert "PID" in result["message"]
        assert controller.calls == []

    def test_should_not_kill_process(self, tmp_path: Any) -> None:
        """守护者**没有** kill 能力 —— 这是架构红线 R2。"""
        guardian = Guardian(_config(tmp_path), controller=FakeController())
        for forbidden in ("kill", "terminate", "kill_process"):
            assert not hasattr(guardian.controller, forbidden)


# ---------------------------------------------------------------------------
# 目标 PID 失效后的重新解析
# ---------------------------------------------------------------------------
class _DeadPidController:
    """第一次调用失败（进程没了），之后都成功。

    :param ok_after: 第几次调用开始成功。
    """

    def __init__(self, ok_after: int = 2) -> None:
        self.calls: List[Tuple[str, int, str]] = []
        self.ok_after = ok_after
        self.prefer = "auto"

    def intervene(self, pid: int, action: str = "") -> Dict[str, Any]:
        """前几次报「进程不存在」，之后正常成功。"""
        self.calls.append(("intervene", int(pid), action))
        ok = len(self.calls) >= self.ok_after
        return {
            "ok": ok,
            "action": "intervene",
            "pid": int(pid),
            "detail": "ok" if ok else "",
            "error": "" if ok else "NoSuchProcess: process PID not found",
        }


class TestPidReResolve:
    """原 PID 失效时自动重新解析 —— 实机踩坑逼出来的能力。

    2026-10 实测 codex 时发现：启动器认错了进程（抓到的是临时
    ``pwsh.exe``），等红线命中要挂起时那个 pid 已经没了。
    没有这道保险，红线就等于白设。
    """

    def test_should_re_resolve_when_process_gone(self, tmp_path: Any) -> None:
        """进程没了要重新解析一次，再挂起新 PID。"""
        controller = _DeadPidController(ok_after=2)
        guardian = Guardian(
            _config(tmp_path),
            controller=controller,
            target_pid=1111,
            pid_resolver=lambda: 2222,
        )
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})

        assert result["ok"] is True
        assert [call[1] for call in controller.calls] == [1111, 2222]

    def test_should_update_target_pid_after_re_resolve(
        self, tmp_path: Any
    ) -> None:
        """重新解析成功后要记住新 PID —— 下次拦截直接用它。"""
        controller = _DeadPidController(ok_after=2)
        guardian = Guardian(
            _config(tmp_path),
            controller=controller,
            target_pid=1111,
            pid_resolver=lambda: 2222,
        )
        guardian.intervene({"rule_id": "catastrophic_root_delete"})

        assert guardian.resolve_pid() == 2222

    def test_should_not_re_resolve_for_other_failures(
        self, tmp_path: Any
    ) -> None:
        """不是「进程没了」的失败（比如权限不足）不重试。"""
        controller = FakeController(ok=False)
        calls: List[int] = []

        def _resolver() -> int:
            calls.append(1)
            return 2222

        guardian = Guardian(
            _config(tmp_path),
            controller=controller,
            target_pid=1111,
            pid_resolver=_resolver,
        )
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})

        assert result["ok"] is False
        assert calls == [], "非「进程没了」的失败不该重新解析"

    def test_should_degrade_when_resolver_raises(self, tmp_path: Any) -> None:
        """重新解析回调自己炸了 —— 降级，不把异常带出去。"""

        def _boom() -> int:
            raise RuntimeError("boom")

        controller = _DeadPidController(ok_after=99)
        guardian = Guardian(
            _config(tmp_path),
            controller=controller,
            target_pid=1111,
            pid_resolver=_boom,
        )
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})

        assert result["ok"] is False
        assert "NoSuchProcess" in result["error"]

    def test_should_protect_re_resolved_pid(self, tmp_path: Any) -> None:
        """重新解析出来的 PID 也要过「保护进程」检查。"""
        controller = _DeadPidController(ok_after=2)
        guardian = Guardian(
            _config(tmp_path),
            controller=controller,
            target_pid=1111,
            pid_resolver=os.getpid,
        )
        result = guardian.intervene({"rule_id": "catastrophic_root_delete"})

        assert result["ok"] is False
        assert len(controller.calls) == 1, "哨兵自身不能被当成拦截目标"


# ---------------------------------------------------------------------------
# 用户否决权
# ---------------------------------------------------------------------------
class TestOverride:
    """用户放行。"""

    def test_override_should_resume_and_blacklist(self, tmp_path: Any) -> None:
        """放行要恢复进程，并把该规则从本次任务中移除。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        guardian.intervene({"rule_id": "catastrophic_root_delete"})

        result = guardian.override({"rule_id": "catastrophic_root_delete"})
        assert result["ok"] is True
        assert result["resumed"] is True
        assert ("resume", 4242, "") in controller.calls
        assert guardian.is_allowed("catastrophic_root_delete") is True

    def test_override_should_prevent_second_intervention(
        self, tmp_path: Any
    ) -> None:
        """放行之后，同一规则不再触发拦截。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        guardian.intervene({"rule_id": "catastrophic_root_delete"})
        guardian.override({"rule_id": "catastrophic_root_delete"})

        controller.calls.clear()
        result = guardian.intervene(
            {"rule_id": "catastrophic_root_delete"}
        )
        assert result["ok"] is False
        assert result["skipped"] is True
        assert controller.calls == []

    def test_override_should_target_last_intervention(self, tmp_path: Any) -> None:
        """不指定 rule_id 时放行最近一次拦截。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        guardian.intervene({"rule_id": "catastrophic_root_delete"})
        result = guardian.override({})
        assert result["ok"] is True
        assert result["rule_id"] == "catastrophic_root_delete"

    def test_override_should_write_audit_files(self, tmp_path: Any) -> None:
        """放行必须留痕。"""
        controller = FakeController()
        config = _config(tmp_path)
        guardian = Guardian(config, controller=controller)
        guardian.intervene({"rule_id": "catastrophic_root_delete"})
        guardian.override({"rule_id": "catastrophic_root_delete", "source": "gui"})

        override_log = tmp_path / "guardian_overrides.json"
        assert override_log.exists()
        payload = json.loads(override_log.read_text(encoding="utf-8"))
        decisions = payload["decisions"]
        assert any(item["action"] == "override" for item in decisions)
        override_entry = [
            item for item in decisions if item["action"] == "override"
        ][0]
        assert override_entry["rule_id"] == "catastrophic_root_delete"
        assert override_entry["source"] == "gui"

        audit_log = tmp_path / "guardian.log"
        assert audit_log.exists()
        text = audit_log.read_text(encoding="utf-8")
        assert "override" in text
        assert "catastrophic_root_delete" in text

    def test_allowed_rules_should_never_be_intervened(self, tmp_path: Any) -> None:
        """配置里的永久白名单优先于一切。"""
        controller = FakeController()
        config = _config(
            tmp_path,
            guardian={"allowed_rules": ["catastrophic_disk_wipe"]},
        )
        guardian = Guardian(config, controller=controller)
        result = guardian.intervene({"rule_id": "catastrophic_disk_wipe"})
        assert result["ok"] is False
        assert result["skipped"] is True
        assert controller.calls == []


# ---------------------------------------------------------------------------
# 平台差异：Windows 绝不终止进程
# ---------------------------------------------------------------------------
class TestWindowsSafety:
    """Windows 上不能把「中断」做成「终止」。"""

    def test_interrupt_must_not_call_os_kill_on_windows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows 上 interrupt 绝不能碰 ``os.kill``。

        CPython 在 Windows 上会把 ``os.kill(pid, SIGINT)`` 翻译成
        ``TerminateProcess`` —— 那不是中断，是**终止**。
        """
        called: List[Any] = []
        monkeypatch.setattr(
            guardian_module.os, "kill", lambda *a, **k: called.append(a)
        )
        monkeypatch.setattr(
            ProcessController, "is_windows", staticmethod(lambda: True)
        )
        controller = ProcessController()
        result = controller.interrupt(4242)
        assert result["ok"] is False
        assert called == [], "Windows 上绝不允许调用 os.kill"

    def test_auto_action_should_downgrade_to_suspend_on_windows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows 上 auto 策略必须降级为挂起。"""
        recorded: List[Tuple[str, int]] = []

        class RecordingController(ProcessController):
            def suspend(self, pid: int) -> Dict[str, Any]:
                recorded.append(("suspend", pid))
                return {
                    "ok": True,
                    "action": "suspend",
                    "pid": pid,
                    "detail": "挂起",
                    "error": "",
                }

        monkeypatch.setattr(
            ProcessController, "is_windows", staticmethod(lambda: True)
        )
        controller = RecordingController(prefer="auto")
        outcome = controller.intervene(4242)
        assert outcome["action"] == "suspend"
        assert recorded == [("suspend", 4242)]

    def test_sigint_should_downgrade_to_suspend_on_windows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """即便显式要求 sigint，Windows 上也必须降级。"""
        monkeypatch.setattr(
            ProcessController, "is_windows", staticmethod(lambda: True)
        )
        controller = ProcessController(prefer="sigint")
        outcome = controller.intervene(4242)
        assert outcome["action"] == "suspend"


# ---------------------------------------------------------------------------
# 引擎路由
# ---------------------------------------------------------------------------
class TestEngineRouting:
    """引擎把红线事件交给守护者。"""

    @staticmethod
    def _engine_event(command: str) -> Dict[str, Any]:
        """构造一个工具调用事件。"""
        return {
            "type": "tool_call",
            "tool": {"name": "Bash", "command": command, "arguments": {}},
        }

    def test_engine_should_route_red_line_to_guardian(self, tmp_path: Any) -> None:
        """红线命令走到引擎时，守护者必须收到干预请求。"""
        from src.adapters.mcode import McodeAdapter
        from src.core.engine import SentinelEngine

        log_path = tmp_path / "agent.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "type": "tool_call",
                    "tool_call": {
                        "name": "Bash",
                        "arguments": {"command": "rm -rf /"},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine({"rules": {"enabled": True}}),
            alert_log_path=str(tmp_path / "alert.log"),
            console=False,
            guardian=guardian,
        )
        try:
            summary = engine.run(str(log_path))
        finally:
            engine.close()

        assert summary["alerts"], "红线事件应该产生告警"
        assert controller.calls, "守护者没有收到干预请求"
        action, pid, _ = controller.calls[0]
        assert action == "intervene"
        assert pid == 4242
        # 告警里要带上守护者的结果，前端才知道该不该亮按钮。
        assert summary["alerts"][0]["guardian"]["ok"] is True

    def test_engine_should_not_route_standard_dangerous(
        self, tmp_path: Any
    ) -> None:
        """普通高危命令不触发干预 —— 守护者不是「更吵的告警器」。"""
        from src.adapters.mcode import McodeAdapter
        from src.core.engine import SentinelEngine

        log_path = tmp_path / "agent.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "type": "tool_call",
                    "tool_call": {
                        "name": "Bash",
                        "arguments": {"command": "chmod 777 /tmp/x"},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine({"rules": {"enabled": True}}),
            alert_log_path=str(tmp_path / "alert.log"),
            console=False,
            guardian=guardian,
        )
        try:
            summary = engine.run(str(log_path))
        finally:
            engine.close()

        assert summary["alerts"], "普通高危命令仍然要告警"
        assert controller.calls == [], "普通高危命令不该触发干预"

    def test_rule_engine_marks_red_line(self) -> None:
        """红线命令的判定结果必须带 ``red_line`` 严重度。"""
        engine = RuleEngine({"rules": {"enabled": True}})
        verdict = engine.check_event(self._engine_event("rm -rf /"))
        assert verdict["verdict"] == VERDICT_DANGEROUS
        assert verdict["severity"] == SEVERITY_RED_LINE

    def test_standard_dangerous_is_not_red_line(self) -> None:
        """普通高危命令不得被当成红线。"""
        engine = RuleEngine({"rules": {"enabled": True}})
        verdict = engine.check_event(self._engine_event("chmod 777 /tmp/x"))
        assert verdict["verdict"] == VERDICT_DANGEROUS
        assert verdict["severity"] != SEVERITY_RED_LINE


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
class TestStatus:
    """状态快照。"""

    def test_status_should_report_enabled(self, tmp_path: Any) -> None:
        """状态里要能看到是否武装。"""
        guardian = Guardian(_config(tmp_path), controller=FakeController())
        status = guardian.status()
        assert status["enabled"] is True
        assert status["mode"] == "guardian"
        assert status["target_pid"] == 4242

    def test_status_should_list_suspended_pids(self, tmp_path: Any) -> None:
        """被挂起的进程要出现在状态里。"""
        guardian = Guardian(_config(tmp_path), controller=FakeController())
        guardian.intervene({"rule_id": "catastrophic_root_delete"})
        assert guardian.status()["suspended_pids"] == [4242]

    def test_close_should_resume_suspended(self, tmp_path: Any) -> None:
        """退出前必须把挂起的进程放回去 —— 不能留个僵死的进程。"""
        controller = FakeController()
        guardian = Guardian(_config(tmp_path), controller=controller)
        guardian.intervene({"rule_id": "catastrophic_root_delete"})
        guardian.close()
        assert ("resume", 4242, "") in controller.calls
        assert guardian.status()["suspended_pids"] == []
