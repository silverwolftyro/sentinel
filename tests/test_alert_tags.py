"""告警标签与解释钩子测试 —— v1.0.0「叫醒服务」的数据基础。

远程推送要知道「是哪个 Agent、它在干什么、为什么被拦」，
这些信息必须**在告警产生的那一刻**就带上 ——
等推送的时候再回头找，已经找不到了。

覆盖：

1. 告警带 ``agent`` / ``task_goal`` / ``triggers``；
2. 守护者**真的拦下来**时才打 ``guardian_intercept`` 标记；
3. 漂移告警带 ``intent_drift`` 标记；
4. 引擎的 ``on_explanation`` 公开钩子可用。
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.adapters.mcode import McodeAdapter
from src.core.channels import TRIGGER_GUARDIAN_INTERCEPT, TRIGGER_INTENT_DRIFT
from src.core.engine import SentinelEngine
from src.rules.basic import RuleEngine


class _FakeGuardian:
    """守护者替身。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: List[Dict[str, Any]] = []

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """返回一次干预结果。"""
        self.calls.append(dict(payload))
        return {
            "ok": self.ok,
            "method": "suspend" if self.ok else "",
            "pid": 1234,
            "error": "" if self.ok else "NoSuchProcess",
        }

    def status(self) -> Dict[str, Any]:
        """返回守护者状态。"""
        return {"ok": True, "enabled": True}

    def override(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """放行。"""
        return {"ok": True}

    def resolve_pid(self) -> Any:
        """没有目标 PID。"""
        return None

    def detect_pid(self, hint: str) -> Any:
        """自动探测不到。"""
        return None

    def set_target_pid(self, pid: int) -> None:
        """设置目标 PID。"""

    def close(self) -> None:
        """关闭。"""


def _make_engine(tmp_path, **kwargs) -> SentinelEngine:
    """造一个引擎（不落盘到真实路径）。"""
    return SentinelEngine(
        McodeAdapter(),
        RuleEngine({}),
        alert_log_path=str(tmp_path / "alerts.log"),
        console=False,
        **kwargs,
    )


def _dangerous_event() -> Dict[str, Any]:
    """一条会命中红线的工具调用事件。"""
    return {
        "type": "tool_call",
        "source": "codex",
        "tool": {
            "name": "shell",
            "command": "rm -rf /tmp/test",
            "arguments": {"command": "rm -rf /tmp/test"},
        },
    }


class TestAlertTags:
    """告警必须自带上下文。"""

    def test_alert_carries_agent_name(self, tmp_path) -> None:
        """★ 告警带 Agent 名称 —— 手机上要写清楚是谁。"""
        engine = _make_engine(tmp_path, agent_name="codex")
        alert = engine._build_alert(_dangerous_event(), {
            "verdict": "DANGEROUS",
            "level": "critical",
            "rule_id": "catastrophic_root_delete",
        })
        assert alert["agent"] == "codex"

    def test_alert_carries_task_goal(self, tmp_path) -> None:
        """★ 告警带任务目标 —— 它本来该干什么。"""
        engine = _make_engine(tmp_path, agent_name="codex")
        engine.intent_tracker = None
        alert = engine._build_alert(_dangerous_event(), {
            "verdict": "DANGEROUS",
            "level": "critical",
            "rule_id": "r",
        })
        assert "task_goal" in alert
        assert alert["task_goal"] == ""

    def test_alert_has_triggers_list(self, tmp_path) -> None:
        """告警带触发类型列表（默认空）。"""
        engine = _make_engine(tmp_path, agent_name="codex")
        alert = engine._build_alert(_dangerous_event(), {
            "verdict": "DANGEROUS",
            "level": "critical",
            "rule_id": "r",
        })
        assert alert["triggers"] == []

    def test_guardian_success_adds_intercept_trigger(self, tmp_path) -> None:
        """★ 守护者真的拦下来了 → 打 ``guardian_intercept`` 标记。"""
        guardian = _FakeGuardian(ok=True)
        engine = _make_engine(tmp_path, agent_name="codex", guardian=guardian)
        alert = engine._build_alert(_dangerous_event(), {
            "verdict": "DANGEROUS",
            "level": "critical",
            "rule_id": "catastrophic_root_delete",
            "severity": "red_line",
        })
        engine._maybe_intervene(alert)
        assert TRIGGER_GUARDIAN_INTERCEPT in alert["triggers"]

    def test_guardian_failure_does_not_add_trigger(self, tmp_path) -> None:
        """★ 拦不住就**不**打标记。

        手机被一条「拦不住」的告警吵醒，比不吵更烦人。
        """
        guardian = _FakeGuardian(ok=False)
        engine = _make_engine(tmp_path, agent_name="codex", guardian=guardian)
        alert = engine._build_alert(_dangerous_event(), {
            "verdict": "DANGEROUS",
            "level": "critical",
            "rule_id": "catastrophic_root_delete",
            "severity": "red_line",
        })
        engine._maybe_intervene(alert)
        assert TRIGGER_GUARDIAN_INTERCEPT not in alert["triggers"]

    def test_standard_severity_never_intervenes(self, tmp_path) -> None:
        """普通告警守护者一眼都不看。"""
        guardian = _FakeGuardian(ok=True)
        engine = _make_engine(tmp_path, agent_name="codex", guardian=guardian)
        alert = engine._build_alert(_dangerous_event(), {
            "verdict": "DANGEROUS",
            "level": "critical",
            "rule_id": "dangerous_command",
            "severity": "standard",
        })
        engine._maybe_intervene(alert)
        assert guardian.calls == []
        assert TRIGGER_GUARDIAN_INTERCEPT not in alert["triggers"]


class TestDriftTrigger:
    """漂移告警带 ``intent_drift`` 标记。"""

    def test_drift_alert_has_drift_trigger(self, tmp_path) -> None:
        """★ 连续意图漂移 → 打 ``intent_drift`` 标记。"""
        from src.core.engine import IntentTracker

        engine = _make_engine(tmp_path, agent_name="codex")
        engine.intent_tracker = IntentTracker(
            goal="创建 hello.py",
            window=5,
            threshold=2,
            enabled=True,
            evaluator=None,
        )
        alert = engine._build_drift_alert({
            "summary": "最近几步都在改 README",
            "reason": "与目标无关",
            "steps": 5,
        })
        assert alert is not None
        assert TRIGGER_INTENT_DRIFT in alert["triggers"]
        assert alert["agent"] == "codex"
        assert alert["task_goal"] == "创建 hello.py"


class TestExplanationHook:
    """引擎的解释补充公开钩子。"""

    def test_hook_is_called_with_alert_id(self, tmp_path) -> None:
        """★ 钩子拿到原告警 ID 与补充字段。

        （事故演练脚本靠它测「解释补充耗时」——
        事后再替换引擎上的私有方法**没用**，
        因为解释线程在构造时就把绑定方法抓住了。）
        """
        seen: List[Any] = []
        engine = _make_engine(
            tmp_path, agent_name="codex", on_explanation=lambda *a: seen.append(a)
        )
        engine._on_explanation(
            "alert-1",
            {"explanation": "人话解释", "explanation_degraded": False},
        )
        assert len(seen) == 1
        assert seen[0][0] == "alert-1"
        assert seen[0][1]["explanation"] == "人话解释"

    def test_hook_failure_does_not_break_engine(self, tmp_path) -> None:
        """观察者抛异常不能影响审查（红线 R5）。"""

        def _boom(*args: Any) -> None:
            raise RuntimeError("boom")

        engine = _make_engine(tmp_path, agent_name="codex", on_explanation=_boom)
        # 不抛异常就算过。
        engine._on_explanation("alert-1", {"explanation": "x"})

    def test_no_hook_is_fine(self, tmp_path) -> None:
        """没挂钩子也正常工作。"""
        engine = _make_engine(tmp_path, agent_name="codex")
        engine._on_explanation("alert-1", {"explanation": "x"})
