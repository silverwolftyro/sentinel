"""v3.2.0 追加回归 —— 「L2 模型判不了」不许静悄悄地失效。

这个缺陷是 **v3.2.0 实机 Night Crawl 演习撞出来的**：

    本机 Ollama 服务在跑，但**一个模型都没装**（``ollama list`` 是空的）。
    于是每一次漂移判定都降级；而降级在口径上等于「没漂移」，
    于是**意图漂移检测静悄悄地死掉** —— 终端不打字、看板不报警、
    晨报里写着「意图漂移 0 次」，看上去一切正常。

    实机证据：演习跑完 3 轮，DRIFT 一次都没判出来；
    终端原文只有一句
    ``调用模型失败（OllamaError: 本地模型不存在：qwen2.5:7b …）``。

修法分两步：

1. **路由层记账** —— :class:`~src.llm.router.ModelRouter` 记录降级次数
   与最近一次原因；
2. **不许沉默** —— 引擎在终端**明确说一声**（同一原因只说一次），
   晨报**如实标注**「那段时间漂移检测等于没开」。

⚠️ 判定口径**一个字没改**：降级依然算「未漂移」（红线 R4），
绝不把「不知道」当成「有问题」。这一版只负责**把失效说出来**。

覆盖清单：

1. 路由层：降级记账（漂移 / 解释两条路）；
2. 路由层：正常判定**不**记账；
3. 引擎：降级结论 → 终端**明确提示**；
4. 引擎：同一个原因**只提示一次**；
5. 引擎：降级结论**不产告警**（红线 R4）；
6. 晨报：Markdown 里**如实标注**降级次数与原因；
7. 晨报摘要：核心三项**不受影响**，但额外带上 ``llm_degraded``；
8. 演习脚本：DRIFT 注入**够长**（≥2 个纯跑题窗口）且**不误伤**其他规则。
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.briefing import (  # noqa: E402
    BriefingStats,
    build_briefing_summary,
    render_markdown,
)
from src.llm.base import DriftResult, Explanation, LLMProvider  # noqa: E402
from src.llm.router import ModelRouter  # noqa: E402

#: 仓库根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Utf8Stream(io.StringIO):
    """带 ``encoding`` 属性的字符串流。

    ⚠️ ``io.StringIO`` 的 ``encoding`` 是 ``None`` ——
    哨兵的 ``_console_safe()`` 会因此认为终端装不下中文，
    把提示降级成 ASCII 摘要。测试要验的是**中文原文**，
    所以这里显式声明 UTF-8。
    """

    @property
    def encoding(self) -> str:
        """永远按 UTF-8 算。"""
        return "utf-8"


# ---------------------------------------------------------------------------
# 假提供方
# ---------------------------------------------------------------------------
class _FakeProvider(LLMProvider):
    """假提供方 —— 可控地「判不了」。"""

    def __init__(
        self,
        name: str = "fake",
        model: str = "fake-model",
        *,
        drift: bool = True,
        degraded: bool = False,
        reason: str = "模型不可用",
    ) -> None:
        super().__init__(model=model, timeout=5.0, enabled=True)
        self.name = name
        self.model = model
        self.drift = drift
        self.degraded = degraded
        self.reason = reason
        self.calls: List[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append("chat")
        return "{}"

    def explain(self, event_data: Dict[str, Any]) -> Explanation:
        self.calls.append("explain")
        return Explanation(
            text="降级文案" if self.degraded else "解释",
            degraded=self.degraded,
            reason=self.reason,
            model=self.model,
            provider=self.name,
        )

    def evaluate_drift(self, goal: str, summary: str) -> DriftResult:
        self.calls.append("drift")
        return DriftResult(
            drift=False if self.degraded else self.drift,
            reason=self.reason,
            degraded=self.degraded,
            model=self.model,
            provider=self.name,
        )


def _event(severity: str = "standard") -> Dict[str, Any]:
    """造一个事件负载。"""
    return {
        "event": {"type": "command_execution", "command": "rm -rf /"},
        "verdict": {
            "verdict": "DANGEROUS",
            "severity": severity,
            "rule_id": "catastrophic_root_delete",
            "reason": "红线级命中",
            "evidence": "rm -rf /",
        },
    }


class TestRouterDegradedAccounting:
    """路由层要**记账** —— 失效了得有人知道。"""

    def test_should_count_degraded_drift(self) -> None:
        """漂移判定降级 → 记一笔。"""
        primary = _FakeProvider("ollama", degraded=True, reason="模型不存在")
        router = ModelRouter(primary, _FakeProvider("openai_compatible"))
        router.evaluate_drift("目标", "摘要")
        assert router.degraded_calls == 1
        assert router.last_degraded_reason == "模型不存在"

    def test_should_not_count_healthy_drift(self) -> None:
        """正常判定**不**记账（别把正常当成异常）。"""
        primary = _FakeProvider("ollama", drift=False)
        router = ModelRouter(primary, _FakeProvider("openai_compatible"))
        router.evaluate_drift("目标", "摘要")
        assert router.degraded_calls == 0
        assert router.last_degraded_reason == ""

    def test_should_count_degraded_explain(self) -> None:
        """解释降级也要记 —— 晨报里的解释同样可能是空的。"""
        primary = _FakeProvider("ollama", degraded=True, reason="模型不可用")
        router = ModelRouter(primary, _FakeProvider("openai_compatible"))
        router.explain(_event("standard"))
        assert router.degraded_calls == 1

    def test_should_keep_accumulating(self) -> None:
        """多次降级要**累加**，不是覆盖。"""
        primary = _FakeProvider("ollama", degraded=True)
        router = ModelRouter(primary, None)
        for _ in range(3):
            router.evaluate_drift("目标", "摘要")
        assert router.degraded_calls == 3

    def test_degraded_still_means_no_drift(self) -> None:
        """⚠️ 口径**一个字没改**：降级 = 未漂移，连续计数归零（红线 R4）。"""
        primary = _FakeProvider("ollama", degraded=True)
        router = ModelRouter(primary, _FakeProvider("openai_compatible"))
        result = router.evaluate_drift("目标", "摘要")
        assert result.drift is False
        assert result.degraded is True
        assert router.drift_streak == 0


class TestEngineDegradedNotice:
    """引擎必须**明确说一声** —— 静悄悄的失效最坏。"""

    @staticmethod
    def _engine() -> Any:
        """造一个只带必要字段的引擎。"""
        from src.core.engine import SentinelEngine

        engine = SentinelEngine.__new__(SentinelEngine)
        engine._llm_degraded_notice = ""
        engine.alerts = []
        return engine

    def test_should_print_degraded_notice(self) -> None:
        """降级结论 → 终端打印明确提示。"""
        engine = self._engine()
        buffer = _Utf8Stream()
        with redirect_stdout(buffer):
            engine._on_drift_result(
                {
                    "drift": False,
                    "reason": "调用模型失败（OllamaError: 本地模型不存在）",
                    "degraded": True,
                    "summary": "摘要",
                    "steps": 5,
                    "alert": False,
                    "consecutive": 0,
                }
            )
        text = buffer.getvalue()
        assert "L2 模型判不了" in text
        assert "本地模型不存在" in text

    def test_should_print_only_once_per_reason(self) -> None:
        """同一个原因**只提醒一次** —— 别把终端刷爆。"""
        engine = self._engine()
        verdict = {
            "drift": False,
            "reason": "模型连续失败已触发熔断，暂时跳过判定",
            "degraded": True,
            "summary": "摘要",
            "steps": 5,
            "alert": False,
            "consecutive": 0,
        }
        buffer = _Utf8Stream()
        with redirect_stdout(buffer):
            engine._on_drift_result(dict(verdict))
            engine._on_drift_result(dict(verdict))
            engine._on_drift_result(dict(verdict))
        assert buffer.getvalue().count("L2 模型判不了") == 1

    def test_should_not_alert_on_degraded(self) -> None:
        """⚠️ 降级**不产告警**（红线 R4：缺失 ≠ 有问题）。"""
        engine = self._engine()
        published: List[Any] = []
        engine._publish_alert = lambda *args, **kwargs: published.append(args)
        buffer = _Utf8Stream()
        with redirect_stdout(buffer):
            engine._on_drift_result(
                {
                    "drift": False,
                    "reason": "模型不可用",
                    "degraded": True,
                    "summary": "摘要",
                    "steps": 5,
                    "alert": False,
                    "consecutive": 0,
                }
            )
        assert published == []


class TestBriefingTellsTheTruth:
    """晨报必须**如实标注** —— 不能粉饰。"""

    @staticmethod
    def _stats(**kwargs: Any) -> BriefingStats:
        """造一份统计。"""
        stats = BriefingStats()
        stats.total_events = 120
        stats.red_line_count = 1
        stats.counts = {"ON_TRACK": 100, "DANGEROUS": 1, "LOOP": 0}
        stats.token_saved = 1234
        for key, value in kwargs.items():
            setattr(stats, key, value)
        return stats

    def test_markdown_should_report_degraded(self) -> None:
        """Markdown 里要有降级次数与原因。"""
        stats = self._stats(llm_degraded_calls=3, llm_degraded_reason="模型不存在")
        text = render_markdown(stats)
        assert "3" in text
        assert "等于没开" in text
        assert "模型不存在" in text

    def test_markdown_should_stay_quiet_when_healthy(self) -> None:
        """正常时不提这事。"""
        text = render_markdown(self._stats())
        assert "等于没开" not in text

    def test_summary_should_keep_core_three(self) -> None:
        """⚠️ 核心三项**不受影响** —— 降级只是额外说明。"""
        stats = self._stats(llm_degraded_calls=2)
        summary = build_briefing_summary(stats)
        assert summary["stats"]["intercepts"] == 1
        assert summary["stats"]["red_line"] == 1
        assert summary["stats"]["token_saved"] == 1234
        assert summary["stats"]["llm_degraded"] == 2

    def test_summary_should_warn_in_markdown(self) -> None:
        """推给手机的报文里也要说。"""
        stats = self._stats(llm_degraded_calls=2)
        summary = build_briefing_summary(stats)
        assert "漂移检测等于没开" in summary["markdown"]

    def test_collect_should_read_router_counters(self) -> None:
        """收集统计时要读路由层的记账。"""
        from src.core.briefing import collect_from_engine

        class _FakeRouter:
            """假路由。"""

            def __init__(self) -> None:
                self.degraded_calls = 5
                self.last_degraded_reason = "熔断了"
                self.models_used = {"ollama:qwen2.5:7b": 5}
                self.escalations: List[Dict[str, Any]] = []

        class _Engine:
            """假引擎。"""

            def __init__(self) -> None:
                self.llm_client = _FakeRouter()
                self.alerts = []
                self.intent_tracker = None

        stats = collect_from_engine(_Engine())
        assert stats.llm_degraded_calls == 5
        assert stats.llm_degraded_reason == "熔断了"


class TestNightCrawlDriftInjection:
    """演习脚本的 DRIFT 注入要**够长**、**不误伤**。"""

    @staticmethod
    def _script() -> Any:
        """加载演习脚本模块。"""
        import importlib.util

        path = os.path.join(
            ROOT, "_构建与发布", "scripts", "final_night_crawl.py"
        )
        spec = importlib.util.spec_from_file_location("fnc_drift", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_should_fill_two_pure_windows(self) -> None:
        """⚠️ 窗口 5 步 × 连续 2 次 → 至少要 10 步连续跑题。"""
        module = self._script()
        lines = module.build_drift_script()
        assert len(lines) >= 10

    def test_should_not_trip_other_rules(self) -> None:
        """注入的跑题动作**不能**顺带触发别的规则 —— 否则计数会脏。"""
        module = self._script()
        from src.rules.basic import RuleEngine

        engine = RuleEngine({})
        for line in module.build_drift_script():
            payload = json.loads(line)
            item = payload.get("item") or {}
            if item.get("type") != "tool_call":
                continue
            command = str(
                (item.get("toolCall") or {}).get("input", {}).get("command", "")
            )
            verdict = engine.check_event(
                {
                    "type": "tool_call",
                    "tool": {"name": "bash", "command": command, "arguments": {}},
                }
            )
            assert verdict.get("verdict") == "ON_TRACK", (
                "跑题注入误触发了规则：{} → {}".format(command, verdict)
            )

    def test_should_only_inject_dangerous_once(self) -> None:
        """危险删除**只注入一次** —— 那是红线，挂起后就没有后续了。"""
        module = self._script()
        lines = module.build_dangerous_script()
        commands = [
            json.loads(line).get("item", {}).get("toolCall", {})
            .get("input", {}).get("command", "")
            for line in lines
            if json.loads(line).get("item", {}).get("type") == "tool_call"
        ]
        deletes = [cmd for cmd in commands if "-delete" in cmd]
        assert len(deletes) == 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
