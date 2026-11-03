"""多级模型路由（v1.2.0）的单元测试。

路由是这一版最需要被钉死的行为，所以用**假提供方**把每条分支都走一遍：

1. 普通告警 → 主力模型；
2. **红线级** → 升级到强模型；
3. **连续漂移** → 升级到强模型；
4. 升级模型降级 → **自动退回主力**（红线事件绝不能没有解释）；
5. 漂移判定失败 → 算「没漂移」，连续计数归零（红线 R4）。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.llm.base import DriftResult, Explanation, LLMProvider  # noqa: E402
from src.llm.router import ESCALATE_DRIFT, ESCALATE_RED_LINE, ModelRouter


class _FakeProvider(LLMProvider):
    """假提供方 —— 记录调用，并按需「降级」。"""

    def __init__(
        self,
        name: str = "fake",
        model: str = "fake-model",
        *,
        drift: bool = True,
        degraded: bool = False,
    ) -> None:
        super().__init__(model=model, timeout=5.0, enabled=True)
        self.name = name
        self.model = model
        self.drift = drift
        self.degraded = degraded
        self.calls: List[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append("chat")
        return "{}"

    def explain(self, event_data: Dict[str, Any]) -> Explanation:
        self.calls.append("explain")
        if self.degraded:
            return Explanation(
                text="降级文案",
                degraded=True,
                reason="模拟失败",
                model=self.model,
                provider=self.name,
            )
        return Explanation(
            text=f"{self.name} 的解释",
            degraded=False,
            model=self.model,
            provider=self.name,
        )

    def evaluate_drift(self, goal: str, summary: str) -> DriftResult:
        self.calls.append("drift")
        if self.degraded:
            return DriftResult(
                drift=False,
                reason="模型不可用",
                degraded=True,
                model=self.model,
                provider=self.name,
            )
        return DriftResult(
            drift=self.drift,
            reason=f"{self.name} 的判定",
            degraded=False,
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


class TestRouting:
    """选谁干活。"""

    def test_should_use_primary_for_normal(self) -> None:
        """普通告警走主力模型。"""
        primary = _FakeProvider("ollama", "qwen2.5:7b")
        fallback = _FakeProvider("openai_compatible", "deepseek-chat")
        router = ModelRouter(primary, fallback)
        result = router.explain(_event("standard"))
        assert result.provider == "ollama"
        assert primary.calls == ["explain"]
        assert fallback.calls == []

    def test_should_escalate_on_red_line(self) -> None:
        """红线级必须升级到强模型。"""
        primary = _FakeProvider("ollama", "qwen2.5:7b")
        fallback = _FakeProvider("openai_compatible", "deepseek-chat")
        router = ModelRouter(primary, fallback)
        result = router.explain(_event("red_line"))
        assert result.provider == "openai_compatible"
        assert fallback.calls == ["explain"]
        assert primary.calls == []
        assert router.escalations
        assert router.escalations[0]["reason"] == ESCALATE_RED_LINE

    def test_should_not_escalate_when_disabled(self) -> None:
        """关了升级开关就不能升级。"""
        primary = _FakeProvider("ollama")
        fallback = _FakeProvider("openai_compatible")
        router = ModelRouter(primary, fallback, escalate_on_red_line=False)
        result = router.explain(_event("red_line"))
        assert result.provider == "ollama"

    def test_should_work_without_fallback(self) -> None:
        """没配升级模型时，行为跟没有路由一样。"""
        primary = _FakeProvider("ollama")
        router = ModelRouter(primary, None)
        result = router.explain(_event("red_line"))
        assert result.provider == "ollama"


class TestDriftEscalation:
    """连续漂移升级。"""

    def test_should_escalate_after_streak(self) -> None:
        """第一次判定走主力；连上之后升级。"""
        primary = _FakeProvider("ollama", drift=True)
        fallback = _FakeProvider("openai_compatible", drift=True)
        router = ModelRouter(primary, fallback, drift_escalate_threshold=1)
        first = router.evaluate_drift("目标", "摘要")
        assert first.provider == "ollama"
        assert router.drift_streak == 1
        second = router.evaluate_drift("目标", "摘要")
        assert second.provider == "openai_compatible"
        assert router.escalations[0]["reason"] == ESCALATE_DRIFT

    def test_should_reset_streak_when_not_drift(self) -> None:
        """没漂移就把连续计数清零。"""
        primary = _FakeProvider("ollama", drift=False)
        fallback = _FakeProvider("openai_compatible")
        router = ModelRouter(primary, fallback, drift_escalate_threshold=1)
        router.evaluate_drift("目标", "摘要")
        assert router.drift_streak == 0

    def test_should_treat_degraded_as_no_drift(self) -> None:
        """⚠️ 模型不可用 → 算「没漂移」，连续计数归零（红线 R4）。"""
        primary = _FakeProvider("ollama", degraded=True)
        fallback = _FakeProvider("openai_compatible")
        router = ModelRouter(primary, fallback, drift_escalate_threshold=1)
        result = router.evaluate_drift("目标", "摘要")
        assert result.drift is False
        assert result.degraded is True
        assert router.drift_streak == 0


class TestFallbackSafety:
    """升级模型挂了怎么办。"""

    def test_should_fall_back_to_primary(self) -> None:
        """云端降级时自动退回主力 —— 红线事件绝不能没有解释。"""
        primary = _FakeProvider("ollama", "qwen2.5:7b")
        fallback = _FakeProvider("openai_compatible", degraded=True)
        router = ModelRouter(primary, fallback)
        result = router.explain(_event("red_line"))
        assert result.provider == "ollama"
        assert result.degraded is False
        assert primary.calls == ["explain"]

    def test_should_survive_provider_exception(self) -> None:
        """子提供方抛异常也不能让哨兵崩。"""

        class _Boom(_FakeProvider):
            def explain(self, event_data: Dict[str, Any]) -> Explanation:
                raise RuntimeError("炸了")

        primary = _FakeProvider("ollama")
        router = ModelRouter(primary, _Boom("openai_compatible"))
        result = router.explain(_event("red_line"))
        # 升级模型直接抛异常 → 退回主力 → 用户照样拿到解释。
        assert result.provider == "ollama"
        assert result.degraded is False
        assert primary.calls == ["explain"]


class TestAccounting:
    """记账 —— 晨报要靠它。"""

    def test_should_record_models_used(self) -> None:
        """用过的模型要记下来。"""
        primary = _FakeProvider("ollama", "qwen2.5:7b")
        fallback = _FakeProvider("openai_compatible", "deepseek-chat")
        router = ModelRouter(primary, fallback)
        router.explain(_event("standard"))
        router.explain(_event("red_line"))
        assert router.models_used == {
            "ollama:qwen2.5:7b": 1,
            "openai_compatible:deepseek-chat": 1,
        }

    def test_should_render_models_text(self) -> None:
        """人话渲染要可读。"""
        primary = _FakeProvider("ollama", "qwen2.5:7b")
        router = ModelRouter(primary, None)
        router.explain(_event("standard"))
        text = router.models_used_text()
        assert "ollama:qwen2.5:7b" in text
        assert "×1" in text

    def test_empty_models_text(self) -> None:
        """没调用过模型时也要有话说。"""
        router = ModelRouter(_FakeProvider("ollama"), None)
        assert "没有调用任何模型" in router.models_used_text()

    def test_reset_should_clear(self) -> None:
        """复位要清干净。"""
        primary = _FakeProvider("ollama")
        router = ModelRouter(primary, None)
        router.explain(_event("standard"))
        router.reset()
        assert router.models_used == {}
        assert router.escalations == []


class TestFactory:
    """工厂装配。"""

    def test_should_not_route_without_fallback(self) -> None:
        """没配 fallback 就不要套路由（行为跟 v1.1.0 一致）。"""
        from src.llm.factory import build_routing_provider

        config = {
            "llm": {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
            }
        }
        provider = build_routing_provider(config)
        assert not isinstance(provider, ModelRouter)

    def test_should_route_with_fallback(self) -> None:
        """配了 fallback 就套上路由。"""
        from src.llm.factory import build_routing_provider

        config = {
            "llm": {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
                "routing": {
                    "enabled": True,
                    "fallback": {
                        "provider": "openai_compatible",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-chat",
                    },
                },
            }
        }
        provider = build_routing_provider(config)
        assert isinstance(provider, ModelRouter)
        assert provider.fallback is not None
        assert provider.fallback.model == "deepseek-chat"

    def test_should_survive_bad_fallback(self) -> None:
        """升级模型配错不能拖垮主流程。"""
        from src.llm.factory import build_routing_provider

        config = {
            "llm": {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
                "routing": {
                    "enabled": True,
                    "fallback": {"provider": "完全不存在的提供方"},
                },
            }
        }
        provider = build_routing_provider(config)
        assert not isinstance(provider, ModelRouter)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
