"""远程告警通道（Webhook）测试 —— v1.0.0「叫醒服务」。

覆盖五件事：

1. 各家推送服务的**报文格式**（钉钉 / 飞书 / Server酱 / Bark / 通用）；
2. **防骚扰** —— 同一事件 60 秒内只推一次；
3. **触发条件** —— 只推「守护者真的拦了」和「连续漂移」；
4. 卡片正文必须带齐**五要素**（Agent / 规则 / 目标 / 操作 / 解释）；
5. 钉钉 / 飞书的**加签**。
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.core import channels
from src.core.channels import (
    DEFAULT_DEDUP_SECONDS,
    TRIGGER_CONTEXT_ANOMALY,
    TRIGGER_GUARDIAN_INTERCEPT,
    TRIGGER_INTENT_DRIFT,
    WebhookChannel,
    format_remote_card,
)

#: 一条典型的红线告警。
RED_LINE_ALERT: Dict[str, Any] = {
    "alert_id": "1793070000123-000001",
    "timestamp": 1793070000.123,
    "verdict": "DANGEROUS",
    "level": "critical",
    "severity": "red_line",
    "rule_id": "catastrophic_root_delete",
    "reason": "红线级命中：递归删除根目录 / 家目录 / 整盘（/）",
    "agent": "codex",
    "task_goal": "在当前目录创建 hello.py 并运行它",
    "command": "rm -rf /tmp/test",
    "triggers": [TRIGGER_GUARDIAN_INTERCEPT],
    "explanation": "AI 想清空整个 /tmp 目录。影响：/tmp 下所有内容会被删除。",
    "guardian": {"ok": True, "method": "suspend", "pid": 25328},
}


class _FakeResponse:
    """假的 HTTP 响应。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeRequests:
    """假的 ``requests`` 模块，记录每次 ``post``。"""

    def __init__(self, status_code: int = 200, error: Any = None) -> None:
        self.status_code = status_code
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: Any = None) -> Any:
        """记录一次投递。"""
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.status_code)


def _install(monkeypatch, fake: _FakeRequests) -> None:
    """把 ``channels.requests`` 换成假模块。"""
    monkeypatch.setattr(channels, "requests", fake)


# ---------------------------------------------------------------------------
# 报文格式
# ---------------------------------------------------------------------------
class TestWebhookFormats:
    """各家推送服务的报文格式。"""

    def test_dingtalk_text_payload(self) -> None:
        """钉钉纯文本。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="dingtalk")
        payload = channel.build_payload(RED_LINE_ALERT)
        assert payload["msgtype"] == "text"
        assert "catastrophic_root_delete" in payload["text"]["content"]

    def test_dingtalk_card_payload(self) -> None:
        """钉钉 Markdown 卡片。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", format="dingtalk", card=True
        )
        payload = channel.build_payload(RED_LINE_ALERT)
        assert payload["msgtype"] == "markdown"
        assert "触发规则" in payload["markdown"]["text"]

    def test_feishu_text_payload(self) -> None:
        """飞书纯文本。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="feishu")
        payload = channel.build_payload(RED_LINE_ALERT)
        assert payload["msg_type"] == "text"
        assert payload["content"]["text"]

    def test_feishu_card_payload(self) -> None:
        """飞书 interactive 卡片，红线用红色模板。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", format="feishu", card=True
        )
        payload = channel.build_payload(RED_LINE_ALERT)
        assert payload["msg_type"] == "interactive"
        assert payload["card"]["header"]["template"] == "red"

    def test_serverchan_payload(self) -> None:
        """Server酱：title + desp。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", format="serverchan"
        )
        payload = channel.build_payload(RED_LINE_ALERT)
        assert set(payload) == {"title", "desp"}
        assert payload["title"].startswith("🚨")

    def test_bark_payload(self) -> None:
        """Bark：critical 用 timeSensitive —— 这才是叫醒服务。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="bark")
        payload = channel.build_payload(RED_LINE_ALERT)
        assert payload["level"] == "timeSensitive"
        assert payload["title"].startswith("🚨")
        assert payload["body"]

    def test_bark_carries_sound(self) -> None:
        """配置了提示音就带上。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", format="bark", sound="siren"
        )
        assert channel.build_payload(RED_LINE_ALERT)["sound"] == "siren"

    def test_generic_payload_has_structured_fields(self) -> None:
        """通用报文带结构化字段（自建网关好解析）。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="generic")
        payload = channel.build_payload(RED_LINE_ALERT)
        assert payload["agent"] == "codex"
        assert payload["rule_id"] == "catastrophic_root_delete"
        assert payload["task_goal"] == "在当前目录创建 hello.py 并运行它"
        assert payload["command"] == "rm -rf /tmp/test"
        assert payload["triggers"] == [TRIGGER_GUARDIAN_INTERCEPT]
        assert payload["alert"]["alert_id"] == RED_LINE_ALERT["alert_id"]

    def test_unknown_format_falls_back_to_generic(self) -> None:
        """不认识的格式按 generic 处理。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="火星信使")
        assert channel.format == "generic"

    def test_serverchan_and_bark_are_known_formats(self) -> None:
        """Server酱 / Bark 是已知格式 —— 不能被当成未知格式。"""
        assert "serverchan" in channels.WEBHOOK_FORMATS
        assert "bark" in channels.WEBHOOK_FORMATS


# ---------------------------------------------------------------------------
# 卡片正文五要素
# ---------------------------------------------------------------------------
class TestRemoteCard:
    """手机上的卡片正文必须信息齐全。"""

    def test_card_contains_five_elements(self) -> None:
        """Agent 名称 / 触发规则 / 任务目标 / 当前操作 / 人话解释。"""
        card = format_remote_card(RED_LINE_ALERT)
        assert "codex" in card
        assert "catastrophic_root_delete" in card
        assert "在当前目录创建 hello.py" in card
        assert "rm -rf /tmp/test" in card
        assert "AI 想清空整个 /tmp 目录" in card

    def test_card_shows_guardian_action(self) -> None:
        """守护者真的动手了要写出来。"""
        card = format_remote_card(RED_LINE_ALERT)
        assert "挂起" in card
        assert "25328" in card

    def test_card_survives_missing_fields(self) -> None:
        """缺字段也不能抛异常。"""
        card = format_remote_card({})
        assert "未知 Agent" in card
        assert "（未设定）" in card
        assert "（无命令）" in card

    def test_card_handles_non_dict(self) -> None:
        """非字典输入按空字典处理。"""
        assert "未知 Agent" in format_remote_card(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 防骚扰
# ---------------------------------------------------------------------------
class TestDedup:
    """同一事件 60 秒内不重复推。

    ⚠️ v1.1.0 起 webhook 是**后台投递**的（不阻塞主事件流），
    所以断言「实际发了几次」之前必须 :meth:`WebhookChannel.flush` 等它跑完。
    """

    def test_same_event_is_suppressed(self, monkeypatch) -> None:
        """★ 60 秒内重复事件只推一次。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="https://x.invalid/h")

        assert channel.send(RED_LINE_ALERT) is True
        assert channel.send(RED_LINE_ALERT) is False
        channel.flush()
        assert len(fake.calls) == 1

    def test_different_events_both_go_out(self, monkeypatch) -> None:
        """不同的事件不受影响。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="https://x.invalid/h")

        other = dict(RED_LINE_ALERT)
        other["command"] = "rm -rf /var/lib"
        assert channel.send(RED_LINE_ALERT) is True
        assert channel.send(other) is True
        channel.flush()
        assert len(fake.calls) == 2

    def test_different_agent_both_go_out(self, monkeypatch) -> None:
        """不同 Agent 的同名命令不互相压制。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="https://x.invalid/h")

        other = dict(RED_LINE_ALERT)
        other["agent"] = "mcode"
        assert channel.send(RED_LINE_ALERT) is True
        assert channel.send(other) is True

    def test_window_expires(self, monkeypatch) -> None:
        """窗口过了就又能推了。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        clock = {"now": 1000.0}
        monkeypatch.setattr(channels.time, "monotonic", lambda: clock["now"])
        channel = WebhookChannel(url="https://x.invalid/h", dedup_seconds=60)

        assert channel.send(RED_LINE_ALERT) is True
        clock["now"] += 59
        assert channel.send(RED_LINE_ALERT) is False
        clock["now"] += 2
        assert channel.send(RED_LINE_ALERT) is True
        channel.flush()
        assert len(fake.calls) == 2

    def test_zero_disables_dedup(self, monkeypatch) -> None:
        """``dedup_seconds=0`` 表示不限制。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="https://x.invalid/h", dedup_seconds=0)

        assert channel.send(RED_LINE_ALERT) is True
        assert channel.send(RED_LINE_ALERT) is True

    def test_failed_send_releases_the_slot(self, monkeypatch) -> None:
        """★ 没送到手机就该允许重试 —— 不能因为一次网络抖动就静音一小时。"""
        fake = _FakeRequests(error=OSError("网络不通"))
        _install(monkeypatch, fake)
        # 关掉重试 —— 这里要验的是「占位被撤销」，不是重试行为。
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=0, retry_backoff=0
        )

        assert channel.send(RED_LINE_ALERT) is True
        channel.flush()
        # 第一次投递失败 → 占位被撤销 → 第二次**照样**能入队。
        assert channel.send(RED_LINE_ALERT) is True
        channel.flush()
        assert len(fake.calls) == 2
        assert channel.failed_sends == 2

    def test_default_window_is_60_seconds(self) -> None:
        """默认防骚扰窗口就是 60 秒。"""
        assert DEFAULT_DEDUP_SECONDS == 60.0
        channel = WebhookChannel(url="https://x.invalid/h")
        assert channel.dedup_seconds == 60.0


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------
class TestTriggers:
    """只推「真的要紧」的触发类型。"""

    def test_guardian_intercept_is_pushed(self, monkeypatch) -> None:
        """守护者真的拦了 → 推。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h",
            triggers=[TRIGGER_GUARDIAN_INTERCEPT, TRIGGER_INTENT_DRIFT],
        )
        assert channel.send(RED_LINE_ALERT) is True

    def test_intent_drift_is_pushed(self, monkeypatch) -> None:
        """连续意图漂移 → 推。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h",
            triggers=[TRIGGER_GUARDIAN_INTERCEPT, TRIGGER_INTENT_DRIFT],
        )
        alert = dict(RED_LINE_ALERT)
        alert["triggers"] = [TRIGGER_INTENT_DRIFT]
        assert channel.send(alert) is True

    def test_ordinary_alert_is_not_pushed(self, monkeypatch) -> None:
        """★ 普通告警不推 —— 半夜一条 STUCK 就能把人吵醒。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h",
            triggers=[TRIGGER_GUARDIAN_INTERCEPT, TRIGGER_INTENT_DRIFT],
        )
        alert = dict(RED_LINE_ALERT)
        alert["triggers"] = []
        assert channel.send(alert) is False
        assert fake.calls == []

    def test_context_anomaly_not_pushed_by_default(self, monkeypatch) -> None:
        """默认触发类型里**不含**上下文异常。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h",
            triggers=[TRIGGER_GUARDIAN_INTERCEPT, TRIGGER_INTENT_DRIFT],
        )
        alert = dict(RED_LINE_ALERT)
        alert["triggers"] = [TRIGGER_CONTEXT_ANOMALY]
        assert channel.send(alert) is False

    def test_no_triggers_means_push_everything(self, monkeypatch) -> None:
        """不配 triggers 时保持旧行为 —— 全推。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="https://x.invalid/h")
        alert = dict(RED_LINE_ALERT)
        alert["triggers"] = []
        assert channel.send(alert) is True

    def test_trigger_as_plain_string_is_accepted(self, monkeypatch) -> None:
        """触发类型写成字符串也能认。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", triggers=[TRIGGER_INTENT_DRIFT]
        )
        alert = dict(RED_LINE_ALERT)
        alert["triggers"] = TRIGGER_INTENT_DRIFT
        assert channel.send(alert) is True


# ---------------------------------------------------------------------------
# 加签
# ---------------------------------------------------------------------------
class TestSigning:
    """钉钉 / 飞书的加签。"""

    def test_dingtalk_url_gets_signature(self, monkeypatch) -> None:
        """钉钉：URL 上要带 timestamp 与 sign。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://oapi.dingtalk.com/robot/send?access_token=abc",
            format="dingtalk",
            secret="SEC",
        )
        channel.send(RED_LINE_ALERT)
        channel.flush()
        url = fake.calls[0]["url"]
        assert "timestamp=" in url
        assert "sign=" in url

    def test_feishu_url_gets_signature(self, monkeypatch) -> None:
        """飞书：同样要加签。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            format="feishu",
            secret="SEC",
        )
        channel.send(RED_LINE_ALERT)
        channel.flush()
        url = fake.calls[0]["url"]
        assert "timestamp=" in url
        assert "sign=" in url

    def test_no_secret_means_no_signature(self, monkeypatch) -> None:
        """没配密钥就不加签。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://oapi.dingtalk.com/robot/send?access_token=abc",
            format="dingtalk",
        )
        channel.send(RED_LINE_ALERT)
        channel.flush()
        assert "sign=" not in fake.calls[0]["url"]

    def test_generic_format_never_signs(self, monkeypatch) -> None:
        """通用网关没有加签概念。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", format="generic", secret="SEC"
        )
        channel.send(RED_LINE_ALERT)
        channel.flush()
        assert fake.calls[0]["url"] == "https://x.invalid/h"


# ---------------------------------------------------------------------------
# 健壮性
# ---------------------------------------------------------------------------
class TestWebhookRobustness:
    """通道永远不能把审查侧搞崩。"""

    def test_empty_url_is_refused(self, monkeypatch) -> None:
        """地址为空不发请求。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="")
        assert channel.send(RED_LINE_ALERT) is False
        assert fake.calls == []

    def test_non_http_scheme_is_refused(self, monkeypatch) -> None:
        """只允许 http/https。"""
        fake = _FakeRequests()
        _install(monkeypatch, fake)
        channel = WebhookChannel(url="file:///etc/passwd")
        assert channel.send(RED_LINE_ALERT) is False
        assert fake.calls == []

    def test_network_error_returns_false(self, monkeypatch) -> None:
        """网络异常不抛异常（投递在后台线程里失败）。"""
        fake = _FakeRequests(error=OSError("连不上"))
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=0, retry_backoff=0
        )
        assert channel.send(RED_LINE_ALERT) is True
        channel.flush()
        assert channel.last_delivered is False
        assert "连不上" in channel.last_error

    def test_non_2xx_returns_false(self, monkeypatch) -> None:
        """非 2xx 记为失败。"""
        fake = _FakeRequests(status_code=500)
        _install(monkeypatch, fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=0, retry_backoff=0
        )
        assert channel.send(RED_LINE_ALERT) is True
        channel.flush()
        assert channel.last_delivered is False
        assert "HTTP 500" in channel.last_error

    def test_missing_requests_library_is_handled(self, monkeypatch) -> None:
        """没装 requests 也不能抛异常。"""
        monkeypatch.setattr(channels, "requests", None)
        channel = WebhookChannel(url="https://x.invalid/h")
        assert channel.send(RED_LINE_ALERT) is False

    def test_fingerprint_is_stable(self) -> None:
        """指纹对同一事件必须稳定。"""
        channel = WebhookChannel(url="https://x.invalid/h")
        assert (
            channel.fingerprint(RED_LINE_ALERT)
            == channel.fingerprint(dict(RED_LINE_ALERT))
        )

    def test_fingerprint_differs_by_command(self) -> None:
        """不同命令指纹不同。"""
        channel = WebhookChannel(url="https://x.invalid/h")
        other = dict(RED_LINE_ALERT)
        other["command"] = "rm -rf /var"
        assert channel.fingerprint(RED_LINE_ALERT) != channel.fingerprint(other)
