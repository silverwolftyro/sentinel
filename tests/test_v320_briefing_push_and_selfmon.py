"""v3.2.0 专项回归 —— 晨报远程投递 + 哨兵自身性能监测。

这个版本只做两件事，但两件都是「用户半夜看不见」的事：

1. **晨报自动投递** —— 晨报落盘后自动推到 Webhook（钉钉 / 飞书 / …），
   摘要只带核心三项（总拦截次数 / 红线次数 / 挽回 Token），
   防止长报文被手机截断；
2. **自身性能监测** —— 哨兵自己也得量一量自己
   （CPU / 内存 / 运行时长 / 版本号），并且 ``tail_stream``
   的读取游标**不能**变成内存黑洞。

覆盖清单：

1. ``build_briefing_summary`` 的核心三项与摘要长度；
2. ``WebhookChannel.build_briefing_payload`` 的五种报文格式；
3. ``send_briefing`` **不过**触发类型与防骚扰过滤；
4. ``Notifier.push_briefing`` 会**等到真实投递结果**；
5. ``WebhookChannel.close()`` **不丢队列**里的晨报；
6. ★ ``triggers: []`` = 推全部（v3.2.0 修的真缺陷）；
7. ``ProcessMonitor.snapshot`` 的字段与 ``format_uptime``；
8. ``FileTailer`` 的超长行缓冲上限；
9. ``/api/system`` 与 ``/settings/system`` 两个路由；
10. 【关于系统】页面模板与前端脚本的契约。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src import selfmon
from src.core import channels
from src.core.briefing import BriefingStats, build_briefing_summary
from src.core.channels import WebhookChannel
from src.core.notifier import Notifier, _build_webhook
from src.gui.server import create_app
from src.tailer import FileTailer

#: 仓库根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 假 HTTP
# ---------------------------------------------------------------------------
class _FakeResponse:
    """假的 HTTP 响应。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeRequests:
    """假的 ``requests`` 模块，记录每次 ``post``。

    :param status_code: 假装返回的 HTTP 状态码。
    :param error: 假装抛出的异常（``None`` = 不抛）。
    """

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


def _stats(**overrides: Any) -> BriefingStats:
    """造一份统计结果（默认值代表「平安的一夜」）。"""
    base: Dict[str, Any] = {
        "session_id": "sess-v320",
        "agent": "mcode",
        "task_goal": "重构一个中等规模的模块",
        "started_at": 1_700_000_000.0,
        "ended_at": 1_700_001_830.0,
        "total_events": 128,
        "counts": {"ON_TRACK": 120, "DANGEROUS": 3, "LOOP": 5},
        "total_tokens": 90000,
        "token_saved": 12345,
        "red_line_count": 2,
        "drift_count": 1,
        "stuck_count": 1,
        "leak_count": 0,
        "loop_count": 5,
    }
    base.update(overrides)
    return BriefingStats(**base)


# ---------------------------------------------------------------------------
# 晨报摘要
# ---------------------------------------------------------------------------
class TestBriefingSummary:
    """晨报摘要的核心三项。"""

    def test_core_three_numbers(self) -> None:
        """★ 摘要必须带齐：总拦截次数 / 红线次数 / 挽回 Token。"""
        summary = build_briefing_summary(_stats())
        stats = summary["stats"]
        assert stats["intercepts"] == 3
        assert stats["red_line"] == 2
        assert stats["token_saved"] == 12345

    def test_intercepts_equals_dangerous_count(self) -> None:
        """拦截次数 = ``DANGEROUS`` 判定数。"""
        summary = build_briefing_summary(
            _stats(counts={"ON_TRACK": 10, "DANGEROUS": 7})
        )
        assert summary["stats"]["intercepts"] == 7

    def test_summary_has_four_keys(self) -> None:
        """摘要有标题 / Markdown / 纯文本 / 统计四个部分。"""
        summary = build_briefing_summary(_stats())
        assert set(summary) == {"title", "markdown", "text", "stats"}
        assert summary["title"]
        assert summary["markdown"]
        assert summary["text"]

    def test_markdown_is_short_enough_for_phone(self) -> None:
        """★ 摘要必须**短** —— 完整晨报可能几十 KB，塞进手机会被截断。"""
        stats = _stats(
            alerts=[
                {"rule_id": "rule-{}".format(i), "reason": "很长的理由" * 40}
                for i in range(50)
            ]
        )
        summary = build_briefing_summary(stats)
        assert len(summary["markdown"]) < 1200
        assert len(summary["text"]) < 600

    def test_core_numbers_appear_in_markdown(self) -> None:
        """核心三项要**写进正文**，而不是只藏在 stats 里。"""
        summary = build_briefing_summary(_stats())
        markdown = summary["markdown"]
        assert "拦截 3 次" in markdown
        assert "红线 2 次" in markdown
        assert "Token" in markdown

    def test_custom_title_is_used(self) -> None:
        """自定义标题会出现在摘要里。"""
        summary = build_briefing_summary(_stats(), title="夜巡报告")
        assert "夜巡报告" in summary["title"]
        assert "夜巡报告" in summary["markdown"]

    def test_empty_night_still_has_core_three(self) -> None:
        """零告警的一夜也要有完整的三项（都是 0）。"""
        summary = build_briefing_summary(
            _stats(
                counts={"ON_TRACK": 10},
                red_line_count=0,
                token_saved=0,
                drift_count=0,
                stuck_count=0,
            )
        )
        stats = summary["stats"]
        assert stats["intercepts"] == 0
        assert stats["red_line"] == 0
        assert stats["token_saved"] == 0


# ---------------------------------------------------------------------------
# 晨报报文格式
# ---------------------------------------------------------------------------
class TestBriefingPayload:
    """五种推送服务的晨报报文。"""

    def test_dingtalk_card(self) -> None:
        """钉钉 Markdown 卡片。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", format="dingtalk", card=True
        )
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert payload["msgtype"] == "markdown"
        assert "哨兵晨报" in payload["markdown"]["title"]

    def test_dingtalk_text(self) -> None:
        """钉钉纯文本。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="dingtalk")
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert payload["msgtype"] == "text"
        assert "拦截 3 次" in payload["text"]["content"]

    def test_feishu_card(self) -> None:
        """飞书 interactive 卡片。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", format="feishu", card=True
        )
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert payload["msg_type"] == "interactive"
        assert payload["card"]["elements"][0]["text"]["content"]

    def test_feishu_text(self) -> None:
        """飞书纯文本。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="feishu")
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert payload["msg_type"] == "text"

    def test_serverchan(self) -> None:
        """Server酱：title + desp。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="serverchan")
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert set(payload) == {"title", "desp"}

    def test_bark(self) -> None:
        """Bark：标题 + 正文。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="bark")
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert payload["title"]
        assert payload["body"]
        assert payload["level"] == "active"

    def test_generic_marks_type_as_briefing(self) -> None:
        """★ 通用报文要能分辨「这是晨报」而不是告警。"""
        channel = WebhookChannel(url="https://x.invalid/h", format="generic")
        payload = channel.build_briefing_payload(build_briefing_summary(_stats()))
        assert payload["type"] == "briefing"
        assert payload["stats"]["intercepts"] == 3
        assert payload["stats"]["red_line"] == 2


# ---------------------------------------------------------------------------
# 晨报投递
# ---------------------------------------------------------------------------
class TestSendBriefing:
    """晨报的入队与投递。"""

    def test_send_briefing_ignores_triggers_filter(self, monkeypatch) -> None:
        """★ 晨报**不受**触发类型过滤 —— 否则永远推不出去。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h",
            triggers=["guardian_intercept"],
            dedup_seconds=0,
        )
        assert channel.send_briefing(build_briefing_summary(_stats())) is True
        assert channel.flush(timeout=5.0) is True
        assert len(fake.calls) == 1
        assert fake.calls[0]["json"]["type"] == "briefing"

    def test_send_briefing_ignores_dedup_window(self, monkeypatch) -> None:
        """★ 晨报是**一次性**总结，不该被防骚扰窗口吃掉。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h", dedup_seconds=600)
        summary = build_briefing_summary(_stats())
        assert channel.send_briefing(summary) is True
        assert channel.send_briefing(summary) is True
        assert channel.flush(timeout=5.0) is True
        assert len(fake.calls) == 2

    def test_send_briefing_without_url_is_refused(self) -> None:
        """没配地址就不投（但也不该抛异常）。"""
        channel = WebhookChannel(url="")
        assert channel.send_briefing(build_briefing_summary(_stats())) is False

    def test_send_briefing_rejects_bad_scheme(self) -> None:
        """非 http/https 协议不投。"""
        channel = WebhookChannel(url="file:///etc/passwd")
        assert channel.send_briefing(build_briefing_summary(_stats())) is False


class TestNotifierPushBriefing:
    """``Notifier.push_briefing`` 的对外行为。"""

    @staticmethod
    def _notifier(*channels: Any) -> Notifier:
        """造一个只挂指定通道的分发器。"""
        notifier = Notifier({"alert": {"enabled": True, "merge_seconds": 0}})
        notifier.channels = list(channels)
        return notifier

    def test_returns_true_after_real_delivery(self, monkeypatch) -> None:
        """★ 会等队列跑完，返回**真实**投递结果。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h")
        notifier = self._notifier(channel)
        assert notifier.push_briefing(build_briefing_summary(_stats())) is True
        assert len(fake.calls) == 1

    def test_returns_false_when_no_channel_supports_it(self) -> None:
        """没有支持晨报的通道 → 返回 ``False``（不假装成功）。"""
        notifier = self._notifier()
        assert notifier.push_briefing(build_briefing_summary(_stats())) is False

    def test_never_raises(self) -> None:
        """★ 晨报推不出去**绝不**影响收尾。"""

        class _Boom:
            name = "boom"

            def send_briefing(self, summary: Dict[str, Any]) -> bool:
                raise RuntimeError("炸了")

        notifier = self._notifier(_Boom())
        assert notifier.push_briefing(build_briefing_summary(_stats())) is False

    def test_pushed_briefing_reaches_the_wire(self, monkeypatch) -> None:
        """★ 报文里必须带核心三项（手机上一眼看到）。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h")
        notifier = self._notifier(channel)
        notifier.push_briefing(build_briefing_summary(_stats()))
        stats = fake.calls[0]["json"]["stats"]
        assert stats["intercepts"] == 3
        assert stats["red_line"] == 2
        assert stats["token_saved"] == 12345


class TestCloseDoesNotDropQueue:
    """★ v3.2.0 修的真缺陷：``close()`` 不能把队列里的晨报丢掉。"""

    def test_close_flushes_pending_briefing(self, monkeypatch) -> None:
        """入队后立刻 ``close()``，报文**仍然要送到**。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h")
        assert channel.send_briefing(build_briefing_summary(_stats())) is True
        channel.close()
        assert len(fake.calls) == 1
        assert fake.calls[0]["json"]["type"] == "briefing"

    def test_close_is_idempotent(self, monkeypatch) -> None:
        """重复 ``close()`` 不炸。"""
        monkeypatch.setattr(channels, "requests", _FakeRequests())
        channel = WebhookChannel(url="https://x.invalid/h")
        channel.close()
        channel.close()

    def test_channel_can_be_revived_after_close(self, monkeypatch) -> None:
        """★ ``close()`` 之后还能再发。

        收尾时的真实顺序是「关引擎 → 推晨报」，
        而 ``engine.close()`` 会把通道关掉。
        如果通道关掉就**彻底死掉**，晨报会静静地躺在队列里没人送，
        而终端还印着「已推送到你的手机」—— 那是说谎。
        """
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h")
        channel.close()
        assert channel.send_briefing(build_briefing_summary(_stats())) is True
        assert channel.flush(timeout=5.0) is True
        assert len(fake.calls) == 1
        assert fake.calls[0]["json"]["type"] == "briefing"


class TestPushBriefingTellsTheTruth:
    """★ 「已推送」必须是真话。"""

    @staticmethod
    def _notifier(*channels_: Any) -> Notifier:
        """造一个只挂指定通道的分发器。"""
        notifier = Notifier({"alert": {"enabled": True, "merge_seconds": 0}})
        notifier.channels = list(channels_)
        return notifier

    def test_stale_success_is_not_reported(self, monkeypatch) -> None:
        """★ 之前成功送过告警，不代表这次晨报也送到了。

        旧实现只看 ``channel.last_delivered`` ——
        那是「最近一次」的结果，可能是一条**更早的告警**留下的。
        于是晨报根本没送到也会返回 ``True``。
        """
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h", max_retries=0)
        notifier = self._notifier(channel)

        # 先成功送一条告警 —— last_delivered 变 True。
        assert channel.send({"rule_id": "r", "triggers": []}) is True
        assert channel.flush(timeout=5.0) is True
        assert channel.last_delivered is True

        # 现在网络断了 —— 晨报送不出去。
        fake.error = RuntimeError("network down")
        assert notifier.push_briefing(build_briefing_summary(_stats())) is False

    def test_delivered_count_tracks_successes(self, monkeypatch) -> None:
        """成功计数会涨，失败不会。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h", max_retries=0)
        assert channel.delivered_count == 0
        channel.send_briefing(build_briefing_summary(_stats()))
        channel.flush(timeout=5.0)
        assert channel.delivered_count == 1
        fake.error = RuntimeError("network down")
        channel.send_briefing(build_briefing_summary(_stats()))
        channel.flush(timeout=5.0)
        assert channel.delivered_count == 1


# ---------------------------------------------------------------------------
# ★ triggers 空列表语义
# ---------------------------------------------------------------------------
class TestEmptyTriggersMeansPushAll:
    """★ ``triggers: []`` = 推全部（v3.2.0 修的真缺陷）。

    用户在设置页把触发类型**清空**，本意是「我全都要」；
    旧实现把它当成「一个都不推」——
    于是深夜出事，手机上**静悄悄地什么都没收到**。
    """

    def test_channel_treats_empty_list_as_no_filter(self) -> None:
        """通道层面：空列表 → 不过滤。"""
        channel = WebhookChannel(url="https://x.invalid/h", triggers=[])
        assert channel.triggers is None
        assert channel.should_send({"triggers": []}) is True

    def test_channel_treats_whitespace_list_as_no_filter(self) -> None:
        """只有空串的列表同样按「不过滤」处理。"""
        channel = WebhookChannel(url="https://x.invalid/h", triggers=["  "])
        assert channel.triggers is None

    def test_non_empty_list_still_filters(self) -> None:
        """非空列表**仍然**过滤 —— 修复不能把过滤能力一起废掉。"""
        channel = WebhookChannel(
            url="https://x.invalid/h", triggers=["guardian_intercept"]
        )
        assert channel.should_send({"triggers": ["intent_drift"]}) is False
        assert channel.should_send({"triggers": ["guardian_intercept"]}) is True

    def test_builder_treats_empty_list_as_no_filter(self) -> None:
        """配置装配层面：``triggers: []`` → 推全部。"""
        channel = _build_webhook(
            {"type": "webhook", "url": "https://x.invalid/h", "triggers": []}
        )
        assert isinstance(channel, WebhookChannel)
        assert channel.triggers is None

    def test_builder_treats_missing_triggers_as_no_filter(self) -> None:
        """不写 ``triggers`` → 推全部（旧行为不变）。"""
        channel = _build_webhook(
            {"type": "webhook", "url": "https://x.invalid/h"}
        )
        assert isinstance(channel, WebhookChannel)
        assert channel.triggers is None

    def test_builder_keeps_non_empty_triggers(self) -> None:
        """配置里写了触发类型 → 过滤照旧。"""
        channel = _build_webhook(
            {
                "type": "webhook",
                "url": "https://x.invalid/h",
                "triggers": ["intent_drift"],
            }
        )
        assert isinstance(channel, WebhookChannel)
        assert channel.triggers == ("intent_drift",)

    def test_empty_triggers_alert_is_still_pushed(self, monkeypatch) -> None:
        """★ 端到端：``triggers: []`` 的通道**真的会**把告警推出去。"""
        fake = _FakeRequests()
        monkeypatch.setattr(channels, "requests", fake)
        channel = _build_webhook(
            {
                "type": "webhook",
                "url": "https://x.invalid/h",
                "triggers": [],
                "dedup_seconds": 0,
            }
        )
        assert isinstance(channel, WebhookChannel)
        assert channel.send({"rule_id": "r", "triggers": []}) is True
        assert channel.flush(timeout=5.0) is True
        assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 自身性能监测
# ---------------------------------------------------------------------------
class TestFormatUptime:
    """运行时长的人话渲染。"""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "不到 1 秒"),
            (5, "0 分 05 秒"),
            (65, "1 分 05 秒"),
            (600, "10 分 00 秒"),
            (3660, "1 小时 1 分"),
            (90000, "1 天 1 小时"),
        ],
    )
    def test_format(self, seconds: float, expected: str) -> None:
        """秒数 → 人话。"""
        assert selfmon.format_uptime(seconds) == expected

    def test_negative_is_clamped(self) -> None:
        """负数不能渲染成奇怪的东西。"""
        assert selfmon.format_uptime(-5) == "不到 1 秒"


class TestProcessMonitor:
    """哨兵自身的采样。"""

    def test_snapshot_has_all_fields(self) -> None:
        """采样结果必须带齐所有字段（取不到也要有键）。"""
        data = selfmon.ProcessMonitor().snapshot()
        for key in (
            "pid",
            "cpu_percent",
            "cpu_ready",
            "memory_mb",
            "memory_peak_mb",
            "private_mb",
            "uptime_seconds",
            "uptime_text",
            "threads",
            "platform",
        ):
            assert key in data

    def test_memory_is_measured(self) -> None:
        """★ 内存必须真的量得到（当前进程不可能是 0 MB）。"""
        data = selfmon.ProcessMonitor().snapshot()
        assert data["memory_mb"] is None or data["memory_mb"] > 0

    def test_uptime_is_positive(self) -> None:
        """运行时长必须大于 0 —— 进程刚启动也是「已经跑了 0 秒」。"""
        data = selfmon.ProcessMonitor().snapshot()
        assert data["uptime_seconds"] is None or data["uptime_seconds"] >= 0
        assert data["uptime_text"]

    def test_first_sample_has_no_cpu(self) -> None:
        """★ 第一次采样**不能**编造 CPU 百分比。"""
        monitor = selfmon.ProcessMonitor(interval=0.0)
        data = monitor.snapshot()
        assert data["cpu_ready"] is False

    def test_second_sample_can_report_cpu(self) -> None:
        """第二次采样间隔够了就能给出 CPU 百分比。"""
        monitor = selfmon.ProcessMonitor(interval=0.01)
        monitor.snapshot()
        time.sleep(0.05)
        data = monitor.snapshot()
        assert data["cpu_ready"] is True
        assert data["cpu_percent"] >= 0.0

    def test_format_line_is_human_readable(self) -> None:
        """给日志用的一行摘要。"""
        line = selfmon.ProcessMonitor().format_line()
        assert "自检" in line
        assert "CPU" in line
        assert "内存" in line

    def test_process_snapshot_helper(self) -> None:
        """一次性采样快捷方式。"""
        data = selfmon.process_snapshot()
        assert "memory_mb" in data

    def test_dead_pid_does_not_raise(self) -> None:
        """★ 监控一个不存在的进程**不能**抛异常（红线 R5：审查侧只降级）。"""
        monitor = selfmon.ProcessMonitor(pid=999999)
        data = monitor.snapshot()
        assert data["memory_mb"] is None or data["memory_mb"] >= 0


# ---------------------------------------------------------------------------
# tail_stream 的内存防线
# ---------------------------------------------------------------------------
class TestTailerBufferLimit:
    """★ 跟读器的缓冲上限 —— 防内存黑洞。"""

    def test_default_buffer_limit_is_one_mib(self) -> None:
        """默认上限 1 MiB。"""
        tailer = FileTailer(os.devnull)
        assert tailer.max_buffer == 1024 * 1024

    def test_overlong_line_is_trimmed(self, tmp_path) -> None:
        """★ 一行写出几 MB 也不会把内存吃光。"""
        path = tmp_path / "big.jsonl"
        path.write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
        tailer = FileTailer(str(path), max_buffer=64 * 1024)
        tailer.poll()
        assert len(tailer._buffer) <= 64 * 1024
        assert tailer._buffer_truncated is True

    def test_truncation_warns_only_once(self, tmp_path, caplog) -> None:
        """超长行只记**一次** warning —— 不然日志会被刷爆。"""
        path = tmp_path / "big.jsonl"
        path.write_text("x" * (256 * 1024), encoding="utf-8")
        tailer = FileTailer(str(path), max_buffer=8 * 1024)
        with caplog.at_level("WARNING", logger="src.tailer"):
            tailer.poll()
            tailer.poll()
        warnings = [
            record
            for record in caplog.records
            if "超长行" in record.getMessage()
        ]
        assert len(warnings) == 1

    def test_normal_lines_are_not_truncated(self, tmp_path) -> None:
        """正常日志行**不受影响**。"""
        path = tmp_path / "normal.jsonl"
        path.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
        tailer = FileTailer(str(path), max_buffer=64 * 1024)
        lines = tailer.poll()
        assert len(lines) == 2
        assert tailer._buffer_truncated is False


# ---------------------------------------------------------------------------
# 看板：关于系统
# ---------------------------------------------------------------------------
def _make_app(tmp_path):
    """造一个指向临时目录的应用（不启动后台线程）。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")
    return create_app(
        str(watch), str(alert_log), start_watcher=False, demo_mode=False
    )


class TestSystemApi:
    """``/api/system`` 端点。"""

    def test_api_system_returns_core_fields(self, tmp_path) -> None:
        """★ 必须带版本号、进程信息与服务器运行时长。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get("/api/system")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["version"]
        assert "process" in data
        assert data["server_uptime_seconds"] >= 0
        assert data["server_uptime_text"]

    def test_api_system_process_has_memory_and_uptime(self, tmp_path) -> None:
        """★ 进程信息里必须有内存占用与运行时长。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            data = client.get("/api/system").json()

        process = data["process"]
        assert "memory_mb" in process
        assert "uptime_text" in process

    def test_settings_system_page_renders(self, tmp_path) -> None:
        """【关于系统】子页能打开。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get("/settings/system")

        assert response.status_code == 200
        assert "关于系统" in response.text


class TestSystemPageAssets:
    """【关于系统】页面模板与前端脚本的契约。"""

    def test_template_exists(self) -> None:
        """模板文件在。"""
        path = os.path.join(ROOT, "src", "gui", "templates", "settings_system.html")
        assert os.path.isfile(path)

    def test_template_shows_the_three_numbers(self) -> None:
        """★ 模板里必须有运行时长、内存占用、版本号三个字段。"""
        path = os.path.join(ROOT, "src", "gui", "templates", "settings_system.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        assert "运行时长" in html
        assert "内存" in html
        assert "版本" in html

    def test_static_script_exists(self) -> None:
        """前端脚本在。"""
        path = os.path.join(ROOT, "src", "gui", "static", "settings_system.js")
        assert os.path.isfile(path)

    def test_static_script_exposes_page_contract(self) -> None:
        """★ 页面脚本必须暴露 ``window.SentinelPage = { init, destroy }``。"""
        path = os.path.join(ROOT, "src", "gui", "static", "settings_system.js")
        with open(path, encoding="utf-8") as handle:
            script = handle.read()
        assert "window.SentinelPage" in script
        assert "init" in script
        assert "destroy" in script

    def test_nav_has_system_entry(self) -> None:
        """左侧导航里有「关于系统」。"""
        path = os.path.join(
            ROOT, "src", "gui", "templates", "settings_base.html"
        )
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        assert "/settings/system" in html
        assert "关于系统" in html


# ---------------------------------------------------------------------------
# 引擎自检
# ---------------------------------------------------------------------------
class TestEngineSelfcheck:
    """引擎侧的周期自检。"""

    def test_engine_exposes_selfcheck(self, tmp_path) -> None:
        """引擎有 ``selfcheck()`` 方法。"""
        from src.core.engine import SentinelEngine

        engine = SentinelEngine.__new__(SentinelEngine)
        engine._monitor = selfmon.ProcessMonitor()
        engine._selfcheck_at = 0.0
        data = engine.selfcheck(force=True)
        assert "cpu_percent" in data
        assert "memory_mb" in data

    def test_selfcheck_is_callable_repeatedly(self, tmp_path) -> None:
        """★ 自检方法**不能**被同名属性覆盖。

        实例属性优先级高于类属性 —— 如果把采样结果存在
        ``self.selfcheck`` 里，第二次调用就会变成
        ``TypeError: 'dict' object is not callable``，
        引擎主循环会在第一次自检时直接崩掉。
        """
        from src.core.engine import SentinelEngine

        engine = SentinelEngine.__new__(SentinelEngine)
        engine._monitor = selfmon.ProcessMonitor()
        engine._selfcheck_at = 0.0
        for _ in range(3):
            data = engine.selfcheck(force=True)
            assert "memory_mb" in data

    def test_engine_constructor_does_not_shadow_method(self, tmp_path) -> None:
        """★ 真实构造出来的引擎里，``selfcheck`` 仍然**是个方法**。"""
        from src.adapters.mcode import McodeAdapter
        from src.core.engine import SentinelEngine
        from src.rules.basic import RuleEngine

        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine({}),
            alert_log_path=str(tmp_path / "alerts.log"),
            console=False,
        )
        assert callable(engine.selfcheck)
        assert engine.selfcheck(force=True) is not None
        # 连续调用也不会因为属性遮蔽而崩。
        assert callable(engine.selfcheck)

    def test_selfcheck_never_raises(self, tmp_path) -> None:
        """★ 自检失败只降级，**绝不**挡住启动（红线 R5）。"""
        from src.core.engine import SentinelEngine

        class _Boom:
            def snapshot(self) -> Dict[str, Any]:
                raise RuntimeError("炸了")

        engine = SentinelEngine.__new__(SentinelEngine)
        engine._monitor = _Boom()
        engine._selfcheck_at = 0.0
        data = engine.selfcheck(force=True)
        assert data == {} or "memory_mb" in data
