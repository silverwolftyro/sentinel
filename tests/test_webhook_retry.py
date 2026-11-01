"""Webhook 重试与不阻塞测试 —— v1.1.0。

覆盖三件事：

1. **指数退避重试** —— 5xx / 网络错误会重试，4xx 不重试；
2. **不阻塞主事件流** —— :meth:`WebhookChannel.send` 只入队，
   立刻返回（哪怕服务端在超时）；
3. **失败提示清晰** —— 重试耗尽后有**看得懂的中文**提示，
   且失败会**释放**防骚扰占位（没送到手机就该允许再推）。

另外用**真的 HTTP 服务器**跑一遍端到端 —— 打桩测不出网络行为。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import pytest

from src.core import channels
from src.core.channels import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    RETRYABLE_STATUS,
    WebhookChannel,
)

#: 让 ``_构建与发布/scripts`` 里的故障注入服务器可被导入。
_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_构建与发布",
    "scripts",
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from webhook_fault_server import FaultWebhookServer  # noqa: E402

#: 一条典型的红线告警。
ALERT: Dict[str, Any] = {
    "alert_id": "1-000001",
    "verdict": "DANGEROUS",
    "level": "critical",
    "severity": "red_line",
    "rule_id": "catastrophic_root_delete",
    "reason": "红线级命中",
    "agent": "codex",
    "task_goal": "写个 hello",
    "command": "rm -rf /tmp/test",
    "triggers": ["guardian_intercept"],
}


class _FakeResponse:
    """假的 HTTP 响应。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeRequests:
    """假的 ``requests`` 模块 —— 按脚本返回状态码。"""

    def __init__(
        self,
        statuses: Optional[List[int]] = None,
        error: Any = None,
    ) -> None:
        self.statuses = list(statuses or [200])
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: Any = None) -> Any:
        """记录一次投递并按脚本返回结果。"""
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.statuses) - 1)
        return _FakeResponse(self.statuses[index])


# ---------------------------------------------------------------------------
# 指数退避
# ---------------------------------------------------------------------------
class TestExponentialBackoff:
    """失败重试。"""

    def test_server_error_is_retried(self, monkeypatch) -> None:
        """★ 5xx 是服务端临时问题 → 重试到成功。"""
        fake = _FakeRequests(statuses=[500, 500, 200])
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=2, retry_backoff=0.01
        )
        try:
            assert channel.send(ALERT) is True
            channel.flush(timeout=10)
            assert len(fake.calls) == 3
            assert channel.last_delivered is True
        finally:
            channel.close()

    def test_network_error_is_retried(self, monkeypatch) -> None:
        """网络异常也要重试。"""
        fake = _FakeRequests(error=OSError("连不上"))
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=2, retry_backoff=0.01
        )
        try:
            channel.send(ALERT)
            channel.flush(timeout=10)
            assert len(fake.calls) == 3
            assert channel.last_delivered is False
        finally:
            channel.close()

    def test_client_error_is_not_retried(self, monkeypatch) -> None:
        """★ 4xx 是请求本身有问题 → 重试也没用，不浪费时间。"""
        fake = _FakeRequests(statuses=[404])
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=3, retry_backoff=0.01
        )
        try:
            channel.send(ALERT)
            channel.flush(timeout=10)
            assert len(fake.calls) == 1
            assert channel.last_delivered is False
            assert "HTTP 404" in channel.last_error
        finally:
            channel.close()

    def test_rate_limit_is_retried(self, monkeypatch) -> None:
        """429（限流）值得重试。"""
        fake = _FakeRequests(statuses=[429, 200])
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=2, retry_backoff=0.01
        )
        try:
            channel.send(ALERT)
            channel.flush(timeout=10)
            assert len(fake.calls) == 2
            assert channel.last_delivered is True
        finally:
            channel.close()

    def test_backoff_grows(self, monkeypatch) -> None:
        """★ 等待时间要**指数增长**（1s、2s、4s…）。"""
        waits: List[float] = []

        fake = _FakeRequests(statuses=[500, 500, 500])
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=3, retry_backoff=1.0
        )

        def _record(seconds: float) -> bool:
            waits.append(float(seconds))
            return False

        monkeypatch.setattr(channel, "_wait_backoff", _record)
        try:
            channel.send(ALERT)
            channel.flush(timeout=10)
            assert waits == [1.0, 2.0, 4.0]
        finally:
            channel.close()

    def test_retryable_status_set(self) -> None:
        """可重试的状态码集合要覆盖常见的服务端故障。"""
        assert 500 in RETRYABLE_STATUS
        assert 502 in RETRYABLE_STATUS
        assert 503 in RETRYABLE_STATUS
        assert 429 in RETRYABLE_STATUS
        assert 404 not in RETRYABLE_STATUS
        assert 400 not in RETRYABLE_STATUS

    def test_default_retry_settings(self) -> None:
        """默认重试 2 次、退避基数 1 秒。"""
        assert DEFAULT_MAX_RETRIES == 2
        assert DEFAULT_RETRY_BACKOFF == 1.0
        channel = WebhookChannel(url="https://x.invalid/h")
        assert channel.max_retries == 2
        assert channel.retry_backoff == 1.0


# ---------------------------------------------------------------------------
# 不阻塞主事件流
# ---------------------------------------------------------------------------
class TestNonBlocking:
    """投递不能拖慢主循环。"""

    def test_send_returns_immediately(self, monkeypatch) -> None:
        """★ send() 只入队，**不**等网络。"""
        import time as _time

        def _slow_post(url: str, json: Any = None, timeout: Any = None) -> Any:
            _time.sleep(1.5)
            return _FakeResponse(200)

        class _Slow:
            post = staticmethod(_slow_post)

        monkeypatch.setattr(channels, "requests", _Slow())
        channel = WebhookChannel(url="https://x.invalid/h", timeout=5)
        try:
            started = _time.monotonic()
            assert channel.send(ALERT) is True
            elapsed = _time.monotonic() - started
            # 网络要 1.5 秒，但 send() 必须**远快于**它。
            assert elapsed < 0.3, f"send() 花了 {elapsed:.3f}s —— 阻塞主循环了"
        finally:
            channel.close()

    def test_queue_full_is_rejected_not_blocked(self, monkeypatch) -> None:
        """★ 队列满了要**丢弃**而不是堵住。"""
        fake = _FakeRequests(statuses=[200])
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(url="https://x.invalid/h", queue_size=1)
        try:
            # 先占住 worker，让第一条卡在队列里。
            channel._ensure_worker()
            accepted = 0
            rejected = 0
            for index in range(20):
                alert = dict(ALERT)
                alert["command"] = f"rm -rf /tmp/{index}"
                if channel.send(alert):
                    accepted += 1
                else:
                    rejected += 1
            assert rejected > 0, "队列满了必须有拒绝，不能无限堆积"
        finally:
            channel.close()


# ---------------------------------------------------------------------------
# 失败提示
# ---------------------------------------------------------------------------
class TestFailureMessage:
    """重试耗尽后的提示。"""

    def test_chinese_message_is_logged(self, monkeypatch, caplog) -> None:
        """★ 必须有**看得懂**的中文提示。"""
        fake = _FakeRequests(statuses=[500])
        monkeypatch.setattr(channels, "requests", fake)
        with caplog.at_level(logging.WARNING, logger="src.core.channels"):
            channel = WebhookChannel(
                url="https://x.invalid/h", max_retries=1, retry_backoff=0.01
            )
            try:
                channel.send(ALERT)
                channel.flush(timeout=10)
            finally:
                channel.close()
        text = caplog.text
        assert "投递最终失败" in text
        assert "请检查网络连通性" in text
        assert "未能" in text

    def test_failed_send_releases_dedup_slot(self, monkeypatch) -> None:
        """★ 没送到手机 → 撤销防骚扰占位 → 允许再推。"""
        fake = _FakeRequests(statuses=[500])
        monkeypatch.setattr(channels, "requests", fake)
        channel = WebhookChannel(
            url="https://x.invalid/h", max_retries=0, retry_backoff=0.01
        )
        try:
            channel.send(ALERT)
            channel.flush(timeout=10)
            assert channel.failed_sends == 1
            # 占位被撤销了 —— 第二条照样能入队。
            assert channel.send(ALERT) is True
            channel.flush(timeout=10)
            assert len(fake.calls) == 2
        finally:
            channel.close()


# ---------------------------------------------------------------------------
# 配置装配
# ---------------------------------------------------------------------------
class TestNotifierWiring:
    """配置要能传到通道。"""

    def test_retry_options_are_passed(self) -> None:
        """``max_retries`` / ``retry_backoff`` 要装配进去。"""
        from src.core.notifier import Notifier

        notifier = Notifier(
            {
                "alert": {
                    "enabled": True,
                    "min_level": "info",
                    "channels": [
                        {
                            "type": "webhook",
                            "enabled": True,
                            "url": "https://x.invalid/h",
                            "max_retries": 5,
                            "retry_backoff": 2.5,
                        }
                    ],
                }
            }
        )
        try:
            channel = notifier.channels[0]
            assert channel.max_retries == 5
            assert channel.retry_backoff == 2.5
        finally:
            notifier.close()


# ---------------------------------------------------------------------------
# 端到端（真 HTTP）
# ---------------------------------------------------------------------------
class TestEndToEnd:
    """用**真的** HTTP 服务器验证。"""

    def test_flaky_server_eventually_succeeds(self) -> None:
        """★ 前 2 次 500 → 重试到成功，服务器收到 3 次。"""
        server = FaultWebhookServer(flaky_times=2)
        server.start()
        try:
            channel = WebhookChannel(
                url=server.url("/flaky"), timeout=5, max_retries=2, retry_backoff=0.05
            )
            try:
                assert channel.send(ALERT) is True
                assert channel.flush(timeout=20) is True
            finally:
                channel.close()
            assert server.count("/flaky") == 3
            assert channel.last_delivered is True
        finally:
            server.stop()

    def test_always_500_gives_up_after_retries(self) -> None:
        """★ 永远 500 → 重试耗尽后放弃，服务器收到「尝试次数」次。"""
        server = FaultWebhookServer(flaky_times=0)
        server.start()
        try:
            channel = WebhookChannel(
                url=server.url("/always500"),
                timeout=5,
                max_retries=2,
                retry_backoff=0.05,
            )
            try:
                channel.send(ALERT)
                channel.flush(timeout=20)
            finally:
                channel.close()
            assert server.count("/always500") == 3
            assert channel.last_delivered is False
        finally:
            server.stop()

    def test_timeout_does_not_block_the_caller(self) -> None:
        """★ 服务端一直超时 → 调用方**不被卡住**。"""
        server = FaultWebhookServer(timeout_delay=5.0)
        server.start()
        try:
            channel = WebhookChannel(
                url=server.url("/timeout"), timeout=1, max_retries=0
            )
            try:
                started = time.monotonic()
                assert channel.send(ALERT) is True
                elapsed = time.monotonic() - started
                assert elapsed < 0.2, f"send() 花了 {elapsed:.3f}s"
            finally:
                channel.close()
        finally:
            server.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
