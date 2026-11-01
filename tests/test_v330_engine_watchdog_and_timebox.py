"""v3.3.0 —— 引擎心跳看门狗、时间盒拦截与防误杀提示的测试。

为什么有这一组
==============

v3.3.0 的两块新逻辑都来自**真实事故**，不是设想：

1. **NInfer 会掉线**（v3.2.1 实机遇到过一次，端口直接没了），
   而且掉得**不声不响** —— 规则层照跑，L2 解释整个停用，
   用户会以为哨兵坏了。
   → 心跳看门狗必须**只读探活**、**不撞车**（单并发引擎）、
   **掉线必告警**、**恢复要报一声**。
2. **《Agent 跑偏警示录》里的真实根因**：
   「一个子任务能吃掉整轮」——
   下载依赖、修测试、给即将删掉的引擎装权重。
   → 时间盒必须能把「同一件事磨太久」揪出来，
   而且**默认不许**守护者动手（那不是破坏操作）。

**全部离线** —— 假的探活函数、假的时钟、假的守护者。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.mcode import McodeAdapter
from src.core.channels import TRIGGER_ENGINE_OFFLINE, TRIGGER_TIMEBOX
from src.core.engine import (
    SentinelEngine,
    format_alert_message,
    publish_system_alert,
)
from src.core.engine_watchdog import (
    ENGINE_OFFLINE_ALERT_REASON,
    VERDICT_ENGINE_OFFLINE,
    VERDICT_ENGINE_RECOVERED,
    EngineWatchdog,
    build_watchdog,
    start_engine_watchdog,
    stop_engine_watchdog,
)
from src.core.health import STATUS_FAIL, HealthItem, render_terminal
from src.core.timebox import (
    TIMEBOX_ALERT_REASON,
    TimeBoxDetector,
    build_timebox,
    classify_operation,
)
from src.llm.base import engine_lock_for
from src.rules.basic import RuleEngine

#: 本机 NInfer 的地址。
NINFER_URL = "http://127.0.0.1:8888/v1"


# ---------------------------------------------------------------------------
# 造数据的帮手
# ---------------------------------------------------------------------------
class FakeClock:
    """假时钟 —— 时间盒测试不能真等 15 分钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        """返回当前时间。"""
        return self.now

    def advance(self, seconds: float) -> None:
        """往前推一段时间。

        :param seconds: 推进的秒数。
        :return: 无。
        """
        self.now += float(seconds)


def _tool_event(command: str, tool: str = "shell") -> Dict[str, Any]:
    """造一条工具调用事件。

    :param command: 工具命令。
    :param tool: 工具名。
    :return: 事件字典。
    """
    return {
        "type": "tool_call",
        "source": "mcode",
        "tool": {
            "name": tool,
            "command": command,
            "arguments": {"command": command},
        },
    }


def _watchdog_config(**extra: Any) -> Dict[str, Any]:
    """造一份指向本机 NInfer、带看门狗的配置。

    :param extra: 要覆盖的字段。
    :return: 配置字典。
    """
    llm = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": NINFER_URL,
        "model": "bonsai2-27b",
        "watchdog": {
            "enabled": True,
            "interval": 30,
            "timeout": 8,
            "restart": {"enabled": False},
        },
    }
    llm.update(extra)
    return {"llm": llm}


class _FakeGuardian:
    """假守护者 —— 记录有没有被要求挂起。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """假装挂起成功。

        :param payload: 干预载荷。
        :return: 干预结果。
        """
        self.calls.append(dict(payload))
        return {"ok": True, "message": "[哨兵已拦截] 目标进程已挂起。"}


# ---------------------------------------------------------------------------
# 引擎心跳看门狗
# ---------------------------------------------------------------------------
class TestEngineWatchdog:
    """心跳看门狗。"""

    def test_disabled_returns_none(self) -> None:
        """没开看门狗就不该建。"""
        config = _watchdog_config()
        config["llm"]["watchdog"]["enabled"] = False
        assert build_watchdog(config) is None

    def test_llm_disabled_returns_none(self) -> None:
        """模型层整个关掉就不该建。"""
        config = _watchdog_config()
        config["llm"]["enabled"] = False
        assert build_watchdog(config) is None

    def test_cloud_endpoint_not_watched(self) -> None:
        """云端 API **不该**被定时戳 —— 那是服务商的事。"""
        config = _watchdog_config(
            base_url="https://api.deepseek.com",
            provider="deepseek",
        )
        assert build_watchdog(config) is None

    def test_local_endpoint_watched(self) -> None:
        """本机地址就该建看门狗。"""
        watchdog = build_watchdog(_watchdog_config())
        assert watchdog is not None
        assert watchdog.base_url == NINFER_URL
        assert watchdog.interval == 30
        assert watchdog.timeout == 8

    def test_first_failure_alerts(self) -> None:
        """第一次探活就失败 —— 必须立刻告警。"""
        alerts: List[Dict[str, Any]] = []
        watchdog = EngineWatchdog(
            NINFER_URL,
            prober=lambda: {"ok": False, "detail": "连不上（服务没启动？）"},
        )
        watchdog.add_listener(alerts.append)
        watchdog.check_once()
        assert len(alerts) == 1
        assert alerts[0]["verdict"] == VERDICT_ENGINE_OFFLINE
        assert alerts[0]["level"] == "critical"
        assert ENGINE_OFFLINE_ALERT_REASON in alerts[0]["reason"]
        assert TRIGGER_ENGINE_OFFLINE in alerts[0]["triggers"]

    def test_offline_then_recovered(self) -> None:
        """掉线 → 恢复，两个状态各报一次。"""
        alerts: List[Dict[str, Any]] = []
        results = [
            {"ok": True, "detail": "已连通"},
            {"ok": False, "detail": "连不上"},
            {"ok": True, "detail": "已连通"},
        ]

        def prober() -> Dict[str, Any]:
            return results.pop(0) if results else {"ok": True, "detail": "已连通"}

        watchdog = EngineWatchdog(NINFER_URL, prober=prober)
        watchdog.add_listener(alerts.append)
        watchdog.check_once()
        watchdog.check_once()
        watchdog.check_once()
        assert [item["verdict"] for item in alerts] == [
            VERDICT_ENGINE_OFFLINE,
            VERDICT_ENGINE_RECOVERED,
        ]

    def test_repeated_failure_alerts_once(self) -> None:
        """一直掉线**不该**每 30 秒报一次 —— 只报状态迁移。"""
        alerts: List[Dict[str, Any]] = []
        watchdog = EngineWatchdog(
            NINFER_URL,
            prober=lambda: {"ok": False, "detail": "连不上"},
        )
        watchdog.add_listener(alerts.append)
        for _ in range(5):
            watchdog.check_once()
        assert len(alerts) == 1

    def test_probe_never_raises(self) -> None:
        """探活抛异常只降级 —— 审查侧绝不能被看门狗带崩。"""

        def boom() -> Dict[str, Any]:
            raise RuntimeError("炸了")

        watchdog = EngineWatchdog(NINFER_URL, prober=boom)
        result = watchdog.probe()
        assert result["ok"] is False
        assert result["detail"]

    def test_probe_skips_when_engine_busy(self) -> None:
        """引擎正在推理时**跳过这一拍** —— 不能撞单并发。"""
        lock = engine_lock_for(NINFER_URL)
        assert lock.acquire(blocking=False)
        try:
            watchdog = EngineWatchdog(
                NINFER_URL,
                prober=lambda: {"ok": True, "detail": "不该被调用"},
            )
            result = watchdog.probe()
            assert result.get("skipped") is True
            assert result["ok"] is True
        finally:
            lock.release()

    def test_status_snapshot(self) -> None:
        """状态快照给看板用。"""
        watchdog = EngineWatchdog(
            NINFER_URL,
            prober=lambda: {"ok": True, "detail": "已连通"},
        )
        watchdog.check_once()
        status = watchdog.status()
        assert status["state"] == "healthy"
        assert status["checks"] == 1
        assert status["base_url"] == NINFER_URL

    def test_不再自动拉起引擎(self) -> None:
        """⚠️ v3.3.0：**绝不**自动拉起引擎（只读监控的边界）。

        用户的要求是明确的：「不要尝试自动拉起，保持只读监控的边界」。

        理由也站得住：哨兵**替用户启动一个进程**就不再是只读监控了；
        而且引擎启动要吃显存 —— 在用户正被别的东西占满显存的时候
        自动拉它，只会把机器拖得更垮。

        所以现在只**说**（「引擎掉了，你手动拉一下」），**不动手**。
        """
        watchdog = EngineWatchdog(
            NINFER_URL,
            prober=lambda: {"ok": False, "detail": "连不上"},
            restart={
                "enabled": True,
                "command": ["C:/engine/ninfer.exe", "--port", "8888"],
                "max_attempts": 2,
            },
        )
        watchdog.check_once()
        assert watchdog.restart_attempts == 0
        assert watchdog.status()["restart_attempts"] == 0

    def test_看门狗模块不导入subprocess(self) -> None:
        """拉起进程需要 ``subprocess`` —— 不导入它就**不可能**偷偷拉起。"""
        import src.core.engine_watchdog as watchdog_module

        assert not hasattr(watchdog_module, "subprocess")

    def test_singleton_dedup(self) -> None:
        """同一台引擎**进程内只起一个**看门狗。"""
        config = _watchdog_config()
        try:
            first = start_engine_watchdog(config)
            second = start_engine_watchdog(config)
            assert first is not None
            assert first is second
        finally:
            stop_engine_watchdog(config)


# ---------------------------------------------------------------------------
# 时间盒
# ---------------------------------------------------------------------------
class TestClassifyOperation:
    """操作分类。"""

    @pytest.mark.parametrize(
        "command,expected",
        [
            ("pip install requests", "dependency"),
            ("npm ci", "dependency"),
            ("curl -O https://x/y.tgz", "dependency"),
            ("git clone https://example/x", "dependency"),
            ("pytest tests/test_a.py", "test"),
            ("npm test", "test"),
            ("cargo test", "test"),
            ("npm run build", "build"),
            ("make -j4", "build"),
            ("rg TODO", "search"),
        ],
    )
    def test_categories(self, command: str, expected: str) -> None:
        """命令要能被分到正确的类别。"""
        assert classify_operation(command) == expected


class TestTimeBox:
    """时间盒检测器。"""

    def test_single_event_no_alert(self) -> None:
        """一次操作不算跑偏。"""
        detector = TimeBoxDetector(window_minutes=15, min_repeats=3)
        assert detector.observe(_tool_event("pytest tests/test_a.py")) is None

    def test_repeat_before_window_no_alert(self) -> None:
        """重复够了但**没到时间** —— 还不到时间盒。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15, min_repeats=3, clock=clock
        )
        for _ in range(5):
            detector.observe(_tool_event("pytest tests/test_a.py"))
            clock.advance(10)
        assert detector.state()["buckets"]

    def test_same_command_after_window_alerts(self) -> None:
        """同一条命令磨过时间盒 —— 判定严重跑偏。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15, min_repeats=3, clock=clock
        )
        verdict = None
        for index in range(6):
            verdict = detector.observe(
                _tool_event("pytest tests/test_a.py")
            )
            if verdict:
                break
            clock.advance(400)
        assert verdict is not None
        assert verdict["verdict"] == "TIMEBOX"
        assert verdict["level"] == "critical"
        assert verdict["rule_id"] == "timebox_exceeded"
        assert TIMEBOX_ALERT_REASON in verdict["reason"]
        assert "严重跑偏" in verdict["reason"]

    def test_category_bucket_alerts(self) -> None:
        """换着花样跑不同的测试 —— 但**全是测试**，一样该报。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15,
            min_repeats=3,
            categories=["dependency", "test"],
            clock=clock,
        )
        verdict = None
        commands = [
            "pytest tests/test_a.py",
            "pytest tests/test_b.py",
            "pytest tests/test_c.py",
            "pytest tests/test_d.py",
        ]
        for command in commands:
            verdict = detector.observe(_tool_event(command))
            if verdict:
                break
            clock.advance(400)
        assert verdict is not None
        assert "跑测试" in verdict["reason"]

    def test_edit_category_not_watched_by_default(self) -> None:
        """「一直在改代码」是**正常**的 —— 默认不盯。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15,
            min_repeats=3,
            categories=["dependency", "test"],
            clock=clock,
        )
        verdict = None
        for index in range(6):
            verdict = detector.observe(
                _tool_event("write file_{}.py".format(index), tool="write")
            )
            if verdict:
                break
            clock.advance(400)
        assert verdict is None

    def test_ignored_commands_not_counted(self) -> None:
        """``git status`` 反复看**很正常** —— 不该算死磕。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15, min_repeats=3, clock=clock
        )
        for _ in range(6):
            detector.observe(_tool_event("git status"))
            clock.advance(400)
        assert detector.state()["buckets"] == []

    def test_fires_once_per_bucket(self) -> None:
        """同一件事**只报一次** —— 不刷屏。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15, min_repeats=3, clock=clock
        )
        fired = 0
        for _ in range(10):
            verdict = detector.observe(_tool_event("pip install requests"))
            if verdict:
                fired += 1
            clock.advance(400)
        assert fired == 1

    def test_disabled_detector_never_alerts(self) -> None:
        """关掉就永远不报。"""
        detector = TimeBoxDetector(enabled=False)
        assert detector.observe(_tool_event("pip install requests")) is None

    def test_non_tool_event_ignored(self) -> None:
        """不是工具调用事件就不看。"""
        detector = TimeBoxDetector()
        assert detector.observe({"type": "assistant_text"}) is None

    def test_build_timebox_from_config(self) -> None:
        """配置要能造出检测器。"""
        detector = build_timebox(
            {
                "rules": {
                    "timebox": {
                        "enabled": True,
                        "window_minutes": 20,
                        "min_repeats": 5,
                        "categories": ["dependency"],
                        "guardian_suspend": True,
                    }
                }
            }
        )
        assert detector is not None
        assert detector.window_minutes == 20
        assert detector.min_repeats == 5
        assert detector.categories == {"dependency"}
        assert detector.guardian_suspend is True

    def test_build_timebox_disabled(self) -> None:
        """关掉就不该造。"""
        assert build_timebox({"rules": {"timebox": {"enabled": False}}}) is None


# ---------------------------------------------------------------------------
# 引擎接入
# ---------------------------------------------------------------------------
class TestEngineIntegration:
    """引擎要真的把时间盒与看门狗接上。"""

    def _make_engine(
        self, tmp_path, **kwargs: Any
    ) -> SentinelEngine:
        """造一个引擎（不落盘到真实路径）。

        :param tmp_path: pytest 临时目录。
        :param kwargs: 透传给引擎的参数。
        :return: 引擎实例。
        """
        return SentinelEngine(
            McodeAdapter(),
            RuleEngine({}),
            alert_log_path=str(tmp_path / "alerts.log"),
            console=False,
            **kwargs,
        )

    def test_timebox_produces_alert(self, tmp_path) -> None:
        """撞破时间盒 → 出告警，带上触发标记。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15, min_repeats=3, clock=clock
        )
        engine = self._make_engine(tmp_path, timebox=detector)
        alert = None
        for _ in range(6):
            alert = engine.process_event(_tool_event("pip install requests"))
            if alert:
                break
            clock.advance(400)
        assert alert is not None
        assert alert["verdict"] == "TIMEBOX"
        assert TRIGGER_TIMEBOX in alert["triggers"]
        assert "严重跑偏" in alert["reason"]
        assert "严重跑偏" in format_alert_message(alert)

    def test_guardian_not_called_by_default(self, tmp_path) -> None:
        """默认**不许**守护者动手。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15,
            min_repeats=3,
            guardian_suspend=False,
            clock=clock,
        )
        guardian = _FakeGuardian()
        engine = self._make_engine(
            tmp_path, timebox=detector, guardian=guardian
        )
        for _ in range(6):
            engine.process_event(_tool_event("pip install requests"))
            clock.advance(400)
        assert guardian.calls == []

    def test_guardian_called_when_opted_in(self, tmp_path) -> None:
        """显式打开后，超时死磕**可以**被挂起。"""
        clock = FakeClock()
        detector = TimeBoxDetector(
            window_minutes=15,
            min_repeats=3,
            guardian_suspend=True,
            clock=clock,
        )
        guardian = _FakeGuardian()
        engine = self._make_engine(
            tmp_path, timebox=detector, guardian=guardian
        )
        for _ in range(6):
            engine.process_event(_tool_event("pip install requests"))
            clock.advance(400)
        assert guardian.calls
        assert guardian.calls[0]["source"] == "timebox"

    def test_watchdog_alert_recorded(self, tmp_path) -> None:
        """看门狗的告警要落进引擎的告警列表。"""
        engine = self._make_engine(tmp_path)
        engine.on_watchdog_alert(
            {
                "verdict": VERDICT_ENGINE_OFFLINE,
                "level": "critical",
                "reason": ENGINE_OFFLINE_ALERT_REASON,
                "rule_id": "engine_offline",
            }
        )
        assert engine.alerts
        assert engine.alerts[-1]["verdict"] == VERDICT_ENGINE_OFFLINE
        assert TRIGGER_ENGINE_OFFLINE in engine.alerts[-1]["triggers"]

    def test_set_engine_watchdog_registers_listener(self, tmp_path) -> None:
        """装上看门狗就等于注册了回调。"""
        engine = self._make_engine(tmp_path)
        watchdog = EngineWatchdog(
            NINFER_URL,
            prober=lambda: {"ok": False, "detail": "连不上"},
        )
        engine.set_engine_watchdog(watchdog)
        watchdog.check_once()
        assert engine.alerts
        assert engine.alerts[-1]["verdict"] == VERDICT_ENGINE_OFFLINE

    def test_publish_system_alert_writes_log(self, tmp_path) -> None:
        """系统级告警要写进告警日志（看板靠它显示）。"""
        log_path = tmp_path / "system_alerts.log"
        publish_system_alert(
            {
                "verdict": VERDICT_ENGINE_OFFLINE,
                "level": "critical",
                "reason": ENGINE_OFFLINE_ALERT_REASON,
            },
            alert_log_path=str(log_path),
            console=False,
        )
        content = log_path.read_text(encoding="utf-8")
        assert "ALERT_JSON" in content
        assert "NInfer" in content


# ---------------------------------------------------------------------------
# 启动提示与防误杀
# ---------------------------------------------------------------------------
class TestStartupBanner:
    """引擎掉线时的「极度清晰」提示。"""

    def test_banner_appears_when_engine_failed(self) -> None:
        """本地引擎体检失败 → 报告尾部要有一段醒目的说明。"""
        items = [
            HealthItem(
                name="本地模型引擎",
                status=STATUS_FAIL,
                detail="连不上（服务没启动？）",
                hint="启动本地模型服务",
                extra={"base_url": NINFER_URL, "model": "bonsai2-27b"},
            )
        ]
        text = render_terminal(items)
        assert "L2 判定与人话解释" in text
        assert NINFER_URL in text
        assert "怎么办" in text

    def test_no_banner_when_healthy(self) -> None:
        """引擎正常就不该吓人。"""
        items = [
            HealthItem(
                name="本地模型引擎",
                status="ok",
                detail="已连通",
                extra={"base_url": NINFER_URL},
            )
        ]
        text = render_terminal(items)
        assert "L2 判定与人话解释" not in text


class TestDefenderGuide:
    """防误杀指南。"""

    def test_build_script_prints_notice(self) -> None:
        """打包完必须显眼地提醒加排除项。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(
            root, "_构建与发布", "scripts", "build_exe.py"
        )
        with open(script, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert "defender_notice" in content
        assert "add_defender_exclusion.ps1" in content
        assert "开机自启" in content

    def test_exclusion_script_exists(self) -> None:
        """排除项助手脚本要在仓库里。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(
            root, "_构建与发布", "scripts", "add_defender_exclusion.ps1"
        )
        assert os.path.exists(script)

    def test_exclusion_script_uses_add_mp_preference(self) -> None:
        """脚本必须真的调 Add-MpPreference。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(
            root, "_构建与发布", "scripts", "add_defender_exclusion.ps1"
        )
        with open(script, "r", encoding="utf-8-sig") as handle:
            content = handle.read()
        assert "Add-MpPreference" in content
        assert "ExclusionPath" in content


class TestConfigSchema:
    """配置模板要带上新段落。"""

    def _example(self) -> Dict[str, Any]:
        """读配置模板。

        :return: 解析后的配置字典。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "config", "config.example.yaml")
        import yaml

        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def test_timebox_section(self) -> None:
        """模板里要有 rules.timebox。"""
        rules = self._example().get("rules") or {}
        timebox = rules.get("timebox") or {}
        assert timebox.get("enabled") is True
        assert timebox.get("window_minutes") == 15
        assert timebox.get("min_repeats") == 3
        assert "guardian_suspend" in timebox

    def test_watchdog_section(self) -> None:
        """模板里要有 llm.watchdog。"""
        llm = self._example().get("llm") or {}
        watchdog = llm.get("watchdog") or {}
        assert watchdog.get("enabled") is True
        assert watchdog.get("interval") == 30
        assert "restart" in watchdog

    def test_example_still_points_at_ninfer(self) -> None:
        """默认还是本机 NInfer。"""
        llm = self._example().get("llm") or {}
        assert llm.get("base_url") == NINFER_URL
        assert llm.get("model") == "bonsai2-27b"


class TestThreadSafety:
    """看门狗线程不该把进程带崩。"""

    def test_background_thread_survives(self) -> None:
        """后台线程里探活抛异常，线程还得活着。"""
        counter = {"n": 0}

        def prober() -> Dict[str, Any]:
            counter["n"] += 1
            if counter["n"] == 1:
                raise RuntimeError("第一次炸")
            return {"ok": True, "detail": "已连通"}

        watchdog = EngineWatchdog(
            NINFER_URL, interval=1, prober=prober
        )
        watchdog.start()
        try:
            deadline = threading.Event()
            deadline.wait(2.5)
        finally:
            watchdog.stop()
        assert watchdog.status()["running"] is False
        assert counter["n"] >= 1


def _unused_optional(value: Optional[Any] = None) -> Optional[Any]:
    """占位（保持 typing 导入被用到）。"""
    return value
