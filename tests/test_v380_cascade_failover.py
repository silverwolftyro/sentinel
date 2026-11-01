"""v3.8.0 回归：L2 掉线热切 L3 · L3 回退预算熔断 · 安静回退 L1。

背景
====

v3.7.0 把级联路由的控制阀交到了用户手上（L2 / L3 独立开关、
L1→L3 桥接、拦截策略），但**掉线**这条路还是老的：

L2（本地 NInfer）一旦崩溃，级联只能干瞪眼 —— 直接回退 L1，
用户配置得再漂亮也白搭。于是出现了「本地引擎一挂，整条审查链路
退化成纯正则」的空窗期。

v3.8.0 补上这条断层：

1. **热切路由** —— L2 **临时性**掉线（连不上 / 429 / 503 / 超时 /
   熔断中）且 L3 已启用时，把**原本给 L2 的载荷**原封不动转给 L3，
   并挂上 ``[WARN] L2 掉线，已自动切换至 L3 深度审查``；
2. **预算熔断** —— 窗口内的热切次数或预估费用超阈值时，
   **放弃**这次云端调用、回退 L1 纯规则，并发一条**严重系统告警**；
3. **安静回退** —— L3 也挂了就回退 L1，**不报错、不卡死**。

⚠️ 三条**不可动摇**的原则（红线一字未改）

* **热切是单盲** —— 只有 L3 一票，**绝不亮红灯**；
* **故障切换只改「动作」，不改「判定口径」** ——
  L1/L2/L3 的判定规则、阈值与告警等级**一个都没动**；
* **预算熔断是系统级告警** —— 它**不挂起** Agent
  （说的是「预算用完了」，不是「Agent 干了坏事」）。

本文件把这些**钉死**，一条都不许退化。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.adapters.mcode import McodeAdapter
from src.core.channels import TRIGGER_CASCADE_BUDGET
from src.core.engine import (
    ONTRACK_MARKER,
    SEVERITY_STANDARD,
    VERDICT_CASCADE_BUDGET,
    SentinelEngine,
)
from src.llm.cascade import (
    CASCADE_BUDGET_NOTICE,
    CASCADE_FAILOVER_NOTICE,
    CASCADE_STATUS_FAILOVER,
    CASCADE_STATUS_UNAVAILABLE,
    CascadeResult,
    CascadeRouter,
    CascadeVerdict,
    http_status_of,
    is_transient_reason,
)
from src.rules.basic import RuleEngine

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]


class _FakeProvider:
    """假模型提供方 —— 按剧本返回文本，或抛异常。

    :param verdicts: 依次返回的判定文本（``None`` 表示抛异常）。
    :param raise_with: 要抛的异常对象。
    :param model: 模型名。
    """

    def __init__(self, verdicts=None, raise_with=None, model="fake-model"):
        self.verdicts = list(verdicts or [])
        self.raise_with = raise_with
        self.model = model
        #: 每次调用记一笔（system / user）。
        self.calls = []

    def chat(self, system: str, user: str) -> str:
        """返回剧本里的下一条判定。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: 判定文本。
        :raises Exception: 剧本指定要抛的异常。
        """
        self.calls.append((system, user))
        if self.raise_with is not None:
            raise self.raise_with
        if self.verdicts:
            return self.verdicts.pop(0)
        return '{"verdict":"ON_TRACK","reason":"默认正常"}'


class _FakeHTTPError(Exception):
    """带 ``response.status_code`` 的假 HTTP 异常。

    用来模拟 Ollama 之类客户端抛的**裸** ``requests.HTTPError``
    —— 错误文本里没有「HTTP 503」这种标准形态。

    :param status_code: HTTP 状态码。
    """

    def __init__(self, status_code: int) -> None:
        super().__init__("{} Server Error".format(status_code))
        self.response = type("_Resp", (), {"status_code": status_code})()


class _CapturingHandler(logging.Handler):
    """把日志记录收集起来的处理器。

    :param records: 收集到的记录列表。
    """

    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        """收一条记录。

        :param record: 日志记录。
        """
        self.records.append(record)


def _ontrack_payloads(handler: _CapturingHandler) -> list:
    """从捕获到的日志里挑出 ``ONTRACK_JSON`` 流水行并解析。

    :param handler: 捕获处理器。
    :return: 解析后的流水字典列表。
    """
    payloads = []
    for record in handler.records:
        message = record.getMessage()
        if not message.startswith(ONTRACK_MARKER):
            continue
        raw = message[len(ONTRACK_MARKER):].strip()
        try:
            payloads.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return payloads


class TestFailoverRouting:
    """L2 掉线 → 载荷热切给 L3。"""

    def test_l2_connection_error_hands_payload_to_l3(self):
        """L2 连不上 → 载荷转给 L3，并挂上提示语。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(
            summary="读了 auth.py",
            action="Read core/auth.py",
            l1_verdict="",
            l1_reason="",
        )

        assert result.status == CASCADE_STATUS_FAILOVER
        assert result.failover is True
        assert result.notice == CASCADE_FAILOVER_NOTICE
        # ⚠️ 只有 L3 一票 —— **绝不亮红灯**。
        assert result.red_light is False
        assert result.escalated is False
        assert len(l3.calls) == 1
        # L3 收到的必须是「**原本给 L2 的那份**载荷」。
        assert "Read core/auth.py" in l3.calls[0][1]

    def test_failover_carries_l2_down_reason_to_l3(self):
        """L3 看到的提示里要写清楚「L2 为什么掉线」。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        router.filter(summary="s", action="a")

        assert "L2 掉线" in l3.calls[0][1]
        assert "ConnectionError" in l3.calls[0][1]

    def test_failover_does_not_light_red_light(self):
        """热切**不亮红灯** —— 双盲原则一字未改。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"DANGEROUS","reason":"很危险"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_FAILOVER
        assert result.failover is True
        assert result.bridged is True
        # ⚠️ L3 说有问题，但**只有一票** —— 绝不升级成红线。
        assert result.red_light is False
        assert router.red_lights == 0

    def test_non_transient_failure_does_not_failover(self):
        """L2 只是「输出没法解析」→ 不热切（不值得烧云端的钱）。"""
        l2 = _FakeProvider(["完全不是 JSON"])
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_UNAVAILABLE
        assert result.failover is False
        assert result.notice == ""
        # L3 **一次都没被调用**。
        assert l3.calls == []
        assert router.l3_calls == 0

    def test_failover_disabled_keeps_v370_behaviour(self):
        """``failover_to_l3=False`` → 与 v3.7.0 行为**完全一致**。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3, failover_to_l3=False)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_UNAVAILABLE
        assert result.failover is False
        assert result.notice == ""
        assert l3.calls == []
        assert router.failover_calls == 0

    def test_l3_down_falls_back_to_l1_quietly(self):
        """L3 也挂了 → **安静回退 L1**：不报错、不卡死。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(raise_with=ConnectionError("云端也连不上"))
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_UNAVAILABLE
        assert result.red_light is False
        assert result.budget_exhausted is False
        # 提示语仍然挂上 —— 用户得知道「试过热切」。
        assert result.notice == CASCADE_FAILOVER_NOTICE
        assert "L1" in result.reason

    def test_l3_absent_falls_back_to_l1(self):
        """没配 L3 → 老实回退 L1（没有云端就没有交叉验证）。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        router = CascadeRouter(l2, None)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_UNAVAILABLE
        assert result.failover is False
        assert result.notice == ""


class TestTransientDetection:
    """「临时性」的判定 —— 热切只对临时性掉线生效。"""

    def test_http_503_is_transient(self):
        """503 → 临时性 → 值得热切。"""
        l2 = _FakeProvider(raise_with=_FakeHTTPError(503))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_FAILOVER
        assert "503" in result.l2.reason

    def test_http_429_is_transient(self):
        """429（限流）→ 临时性。"""
        l2 = _FakeProvider(raise_with=_FakeHTTPError(429))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_FAILOVER

    def test_http_400_is_not_transient(self):
        """400（请求本身有问题）→ 不是临时性 → 不热切。"""
        l2 = _FakeProvider(raise_with=_FakeHTTPError(400))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_UNAVAILABLE
        assert result.failover is False
        assert l3.calls == []

    def test_http_status_of_reads_status_code(self):
        """``http_status_of`` 能从裸 ``HTTPError`` 里挖出状态码。"""
        assert http_status_of(_FakeHTTPError(503)) == 503
        assert http_status_of(ValueError("没有状态码")) == 0

    def test_is_transient_reason_markers(self):
        """临时性标志词覆盖连接错误 / 限流 / 服务不可用。"""
        assert is_transient_reason("ConnectionError（连接被拒绝）")
        assert is_transient_reason("HTTP 429：触发限流")
        assert is_transient_reason("503 Service Unavailable")
        assert not is_transient_reason("模型输出无法解析")


class TestFallbackBudget:
    """L3 回退预算熔断（次数 / 金额）。"""

    def _router(self, l2, l3, **kwargs):
        """造一个「L2 必掉线」的路由器。

        :param l2: L2 假提供方。
        :param l3: L3 假提供方。
        :param kwargs: 传给 ``CascadeRouter`` 的额外参数。
        :return: 路由器。
        """
        options = {
            "breaker_threshold": 100,
            "l3_fallback_limit": 100,
            "l3_fallback_limit_seconds": 3600.0,
        }
        options.update(kwargs)
        return CascadeRouter(l2, l3, **options)

    def test_count_limit_melts_down(self):
        """次数超阈值 → 熔断：放弃云端调用，回退 L1。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = self._router(l2, l3, l3_fallback_limit=2)

        first = router.filter(summary="s", action="a")
        second = router.filter(summary="s", action="a")
        third = router.filter(summary="s", action="a")

        assert first.status == CASCADE_STATUS_FAILOVER
        assert second.status == CASCADE_STATUS_FAILOVER
        assert third.status == CASCADE_STATUS_UNAVAILABLE
        assert third.budget_exhausted is True
        assert third.notice == CASCADE_BUDGET_NOTICE
        assert router.budget_exhausted_calls == 1
        # ⚠️ 熔断后**不再调用 L3**。
        assert len(l3.calls) == 2

    def test_cost_limit_melts_down(self):
        """预估费用超阈值 → 熔断。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = self._router(
            l2,
            l3,
            l3_fallback_limit=0,
            l3_fallback_cost_limit=0.5,
            l3_fallback_unit_cost=0.2,
        )

        for _ in range(3):
            router.filter(summary="s", action="a")
        result = router.filter(summary="s", action="a")

        assert result.budget_exhausted is True
        assert result.notice == CASCADE_BUDGET_NOTICE
        assert "费用" in result.reason
        assert len(l3.calls) == 3

    def test_meltdown_does_not_light_red_light(self):
        """熔断**不亮红灯** —— 预算问题不是 Agent 的错。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = self._router(l2, l3, l3_fallback_limit=1)

        router.filter(summary="s", action="a")
        result = router.filter(summary="s", action="a")

        assert result.budget_exhausted is True
        assert result.red_light is False
        assert router.red_lights == 0

    def test_budget_resets(self):
        """``reset()`` 之后预算窗口清空，热切重新可用。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = self._router(l2, l3, l3_fallback_limit=1)

        router.filter(summary="s", action="a")
        assert router.filter(summary="s", action="a").budget_exhausted is True

        router.reset()
        result = router.filter(summary="s", action="a")

        assert result.status == CASCADE_STATUS_FAILOVER
        assert router.failover_calls == 1

    def test_stats_expose_failover_and_budget(self):
        """统计里要能看到热切与预算的用量。"""
        l2 = _FakeProvider(raise_with=ConnectionError("连接被拒绝"))
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = self._router(l2, l3, l3_fallback_limit=5, l3_fallback_unit_cost=0.1)

        router.filter(summary="s", action="a")
        stats = router.stats()

        assert stats["failover_to_l3"] is True
        assert stats["failover"] == 1
        assert stats["budget_exhausted"] == 0
        assert "ConnectionError" in stats["last_failover_reason"]
        budget = stats["l3_fallback_budget"]
        assert budget["limit"] == 5
        assert budget["used"] == 1
        assert abs(budget["used_cost"] - 0.1) < 1e-9


class TestEngineCascadeNotice:
    """引擎侧：热切提示进流水，预算熔断发严重告警。"""

    def _engine(self, tmp_path, router):
        """造一个带捕获 logger 的引擎。

        :param tmp_path: pytest 临时目录。
        :param router:  级联路由器。
        :return: ``(engine, handler)``。
        """
        logger = logging.getLogger(
            "sentinel.test.v380.cascade.{}".format(id(tmp_path))
        )
        logger.handlers = []
        logger.propagate = False
        handler = _CapturingHandler()
        logger.addHandler(handler)
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine(config={}),
            alert_log_path=str(tmp_path / "sentinel_alert.log"),
            console=False,
            alert_logger=logger,
        )
        engine.set_cascade(router)
        return engine, handler

    def test_failover_notice_is_written_to_stream(self, tmp_path):
        """热切提示要写成一行流水，带 ``audit_text``。"""
        router = CascadeRouter(
            _FakeProvider(raise_with=ConnectionError("连接被拒绝")),
            _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}']),
        )
        engine, handler = self._engine(tmp_path=tmp_path, router=router)
        try:
            engine.alerts.append(
                {
                    "alert_id": "A-1",
                    "severity": "HIGH",
                    "verdict": "DRIFT",
                    "fingerprint": "fp-1",
                }
            )
            result = CascadeResult(
                l2=CascadeVerdict(
                    available=False, reason="ConnectionError", transient=True
                ),
                l3=CascadeVerdict(verdict="ON_TRACK", available=True),
                status=CASCADE_STATUS_FAILOVER,
                failover=True,
                bridged=True,
                notice=CASCADE_FAILOVER_NOTICE,
            )
            engine._on_cascade_result("A-1", result)

            payloads = [
                item
                for item in _ontrack_payloads(handler)
                if item.get("event_type") == "cascade_notice"
            ]
            assert payloads, "热切提示没有进流水"
            payload = payloads[0]
            assert payload["summary"] == CASCADE_FAILOVER_NOTICE
            assert payload["audit_text"] == CASCADE_FAILOVER_NOTICE
            assert payload["audit_level"] == "warn"
            assert payload["fingerprint"] == "fp-1"
        finally:
            engine.close()

    def test_failover_does_not_upgrade_alert_severity(self, tmp_path):
        """热切**不升级**原告警等级。"""
        router = CascadeRouter(
            _FakeProvider(raise_with=ConnectionError("连接被拒绝")),
            _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}']),
        )
        engine, _ = self._engine(tmp_path=tmp_path, router=router)
        try:
            engine.alerts.append(
                {"alert_id": "A-2", "severity": "HIGH", "verdict": "DRIFT"}
            )
            result = CascadeResult(
                l2=CascadeVerdict(
                    available=False, reason="ConnectionError", transient=True
                ),
                l3=CascadeVerdict(verdict="ON_TRACK", available=True),
                status=CASCADE_STATUS_FAILOVER,
                failover=True,
                bridged=True,
                notice=CASCADE_FAILOVER_NOTICE,
            )
            engine._on_cascade_result("A-2", result)

            alert = engine.alerts[0]
            assert alert["severity"] == "HIGH"
            assert alert.get("escalated") is None
            # 单盲结论**记档**，但不升级。
            assert alert["cascade"]["status"] == CASCADE_STATUS_FAILOVER
        finally:
            engine.close()

    def test_budget_meltdown_emits_critical_system_alert(self, tmp_path):
        """预算熔断 → 一条**严重系统告警**，且**不挂起** Agent。"""
        router = CascadeRouter(
            _FakeProvider(raise_with=ConnectionError("连接被拒绝")),
            _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}']),
        )
        engine, _ = self._engine(tmp_path=tmp_path, router=router)
        try:
            engine.alerts.append(
                {"alert_id": "A-3", "severity": "HIGH", "verdict": "DRIFT"}
            )
            result = CascadeResult(
                l2=CascadeVerdict(
                    available=False, reason="ConnectionError", transient=True
                ),
                status=CASCADE_STATUS_UNAVAILABLE,
                budget_exhausted=True,
                notice=CASCADE_BUDGET_NOTICE,
                reason="L3 回退预算已耗尽（60 秒内热切次数已达上限 1 次），"
                "已回退 L1 纯规则",
            )
            engine._on_cascade_result("A-3", result)

            budget_alerts = [
                item
                for item in engine.alerts
                if item.get("verdict") == VERDICT_CASCADE_BUDGET
            ]
            assert budget_alerts, "预算熔断没有产生系统告警"
            alert = budget_alerts[0]
            assert alert["level"] == "critical"
            assert alert["rule_id"] == "cascade_l3_budget_exhausted"
            # ⚠️ 系统级告警 —— **不挂起** Agent。
            assert alert["severity"] == SEVERITY_STANDARD
            assert alert["triggers"] == [TRIGGER_CASCADE_BUDGET]
            assert alert["event_type"] == "cascade_budget"
        finally:
            engine.close()

    def test_budget_notice_level_is_danger(self, tmp_path):
        """熔断提示在流水里是 ``danger`` 颜色（比热切更重）。"""
        router = CascadeRouter(
            _FakeProvider(raise_with=ConnectionError("连接被拒绝")),
            _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}']),
        )
        engine, handler = self._engine(tmp_path=tmp_path, router=router)
        try:
            engine.alerts.append(
                {"alert_id": "A-4", "severity": "HIGH", "verdict": "DRIFT"}
            )
            result = CascadeResult(
                l2=CascadeVerdict(
                    available=False, reason="ConnectionError", transient=True
                ),
                status=CASCADE_STATUS_UNAVAILABLE,
                budget_exhausted=True,
                notice=CASCADE_BUDGET_NOTICE,
                reason="预算耗尽",
            )
            engine._on_cascade_result("A-4", result)

            payloads = [
                item
                for item in _ontrack_payloads(handler)
                if item.get("event_type") == "cascade_notice"
            ]
            assert payloads
            assert payloads[0]["audit_level"] == "danger"
        finally:
            engine.close()


class TestConfigWiring:
    """配置贯通 —— 开关与预算键要能从配置走到路由器。"""

    def test_example_config_has_failover_keys(self):
        """示例配置里必须有热切与预算的键。"""
        text = (ROOT / "config" / "config.example.yaml").read_text(
            encoding="utf-8"
        )
        for key in (
            "failover_to_l3",
            "l3_fallback_limit",
            "l3_fallback_limit_seconds",
            "l3_fallback_cost_limit",
            "l3_fallback_unit_cost",
        ):
            assert key in text, "示例配置缺少键：{}".format(key)

    def test_factory_passes_failover_options(self):
        """工厂要把配置里的热切参数传给路由器。"""
        from src.llm import factory

        source = (ROOT / "src" / "llm" / "factory.py").read_text(
            encoding="utf-8"
        )
        for key in (
            "failover_to_l3",
            "l3_fallback_limit",
            "l3_fallback_limit_seconds",
            "l3_fallback_cost_limit",
            "l3_fallback_unit_cost",
        ):
            assert key in source, "工厂没有透传键：{}".format(key)
        assert factory is not None
