"""告警通道层测试。

全部离线：不弹真窗口、不发声、不发真请求 —— 所有外部依赖一律用
假对象替换，测试不会在开发机上产生任何可见副作用。
"""

from __future__ import annotations

import io
import json
import subprocess

from src.core import channels
from src.core.channels import (
    ConsoleChannel,
    DesktopChannel,
    FileChannel,
    SoundChannel,
    WebhookChannel,
    format_alert,
    level_rank,
)

#: 一条典型的 DANGEROUS 告警（形状来自 ``SentinelEngine._build_alert``）。
ALERT = {
    "timestamp": 1700000000.0,
    "verdict": "DANGEROUS",
    "level": "critical",
    "rule_id": "dangerous_command",
    "reason": "匹配到高危命令: rm -rf",
    "evidence": "rm -rf",
    "source": "mcode",
    "event_type": "tool_call",
    "tool": "Bash",
    "command": "rm -rf /tmp/x",
    "arguments": {"command": "rm -rf /tmp/x"},
    "explanation": "清理临时构建产物。",
    "explanation_degraded": False,
}


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class _FakeResponse:
    """假的 HTTP 响应。"""

    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeRequests:
    """假的 ``requests`` 模块，只记录 ``post`` 调用。"""

    def __init__(self, status_code=200, error=None):
        self.calls = []
        self._response = _FakeResponse(status_code)
        self._error = error

    def post(self, url, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._response


class _FakePopen:
    """假的 ``subprocess.Popen``，记录参数与环境变量。"""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = None
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


class _FakeWinsound:
    """假的 ``winsound`` 模块。"""

    def __init__(self, error=None):
        self.beeps = []
        self._error = error

    def Beep(self, frequency, duration):  # noqa: N802 —— 模仿 winsound 命名
        if self._error is not None:
            raise self._error
        self.beeps.append((frequency, duration))


# ---------------------------------------------------------------------------
# 文本渲染
# ---------------------------------------------------------------------------


def test_format_alert_handles_missing_keys():
    """缺字段的告警字典也必须渲染成合法文本，不抛 KeyError。"""
    text = format_alert({})
    assert "[WARN] UNKNOWN" in text
    assert "工具：-" in text
    assert "命令：-" in text
    assert isinstance(text, str)


def test_format_alert_handles_non_dict():
    """非字典输入按空字典处理。"""
    assert "[WARN] UNKNOWN" in format_alert(None)
    assert "[WARN] UNKNOWN" in format_alert("boom")


def test_format_alert_falls_back_to_reason():
    """没有 L2 解释时，用规则层的 reason 顶上。"""
    text = format_alert(
        {
            "verdict": "DANGEROUS",
            "level": "critical",
            "reason": "匹配到高危命令: rm -rf",
        }
    )
    assert "[CRITICAL] DANGEROUS: 匹配到高危命令: rm -rf" in text
    assert "人话解释：匹配到高危命令: rm -rf" in text


def test_format_alert_marks_degraded_explanation():
    """L2 降级时要在文案里说清楚。"""
    text = format_alert({"reason": "疑似死循环", "explanation_degraded": True})
    assert "降级" in text
    assert "疑似死循环" in text


def test_format_alert_supports_any_verdict():
    """判定名将来会增加（STUCK / LEAK 等），这里按通用字符串处理。"""
    assert "[WARN] STUCK" in format_alert(
        {"verdict": "STUCK", "level": "warn"}
    )
    assert "[WARN] LEAK" in format_alert({"verdict": "LEAK", "level": "warn"})


def test_level_rank_orders_levels():
    """info < warn < critical。"""
    assert level_rank("info") < level_rank("warn")
    assert level_rank("warn") < level_rank("critical")


def test_level_rank_unknown_level_is_not_dropped():
    """无法识别的等级按最高级处理 —— 宁可多报也不漏报。"""
    assert level_rank("catastrophic") == level_rank("critical")


# ---------------------------------------------------------------------------
# 终端通道
# ---------------------------------------------------------------------------


def test_console_channel_writes_to_stream():
    """终端通道把告警写到指定流。"""
    stream = io.StringIO()
    channel = ConsoleChannel(stream=stream)
    try:
        assert channel.send(ALERT) is True
    finally:
        channel.close()

    text = stream.getvalue()
    assert "[CRITICAL] DANGEROUS" in text
    assert "rm -rf /tmp/x" in text


def test_console_channel_never_raises():
    """通道对调用方永远不抛异常。"""
    channel = ConsoleChannel(stream=io.StringIO())
    try:
        assert channel.send({"weird": object()}) is True
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# 文件通道
# ---------------------------------------------------------------------------


def test_file_channel_writes_one_json_object_per_line(tmp_path):
    """每条告警落一行 JSON，且自动创建缺失的父目录。"""
    path = tmp_path / "nested" / "deep" / "alerts.jsonl"
    channel = FileChannel(path=str(path))
    try:
        assert channel.send(ALERT) is True
        assert channel.send(ALERT) is True
    finally:
        channel.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["verdict"] == "DANGEROUS"
    assert first["command"] == "rm -rf /tmp/x"


def test_file_channel_close_is_idempotent(tmp_path):
    """close() 可以重复调用。"""
    channel = FileChannel(path=str(tmp_path / "a.jsonl"))
    channel.send(ALERT)
    channel.close()
    channel.close()


def test_file_channel_handles_unserializable_value(tmp_path):
    """不可序列化的值不能把通道打挂。"""
    channel = FileChannel(path=str(tmp_path / "a.jsonl"))
    try:
        assert channel.send({"obj": object()}) is True
    finally:
        channel.close()


def test_file_channel_write_failure_returns_false(tmp_path):
    """写入失败时返回 False 而不是抛异常。"""
    channel = FileChannel(path=str(tmp_path / "a.jsonl"))
    # 目标路径被占成目录，追加写入必然失败。
    (tmp_path / "a.jsonl").mkdir()
    try:
        assert channel.send(ALERT) is False
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# 桌面通知通道
# ---------------------------------------------------------------------------


def test_desktop_channel_returns_false_without_mechanism(monkeypatch):
    """平台没有任何通知方式时返回 False，不抛异常。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "unknown")
    monkeypatch.setattr(channels, "load_plyer_notifier", lambda: None)
    assert DesktopChannel().send(ALERT) is False


def test_desktop_channel_uses_plyer_when_available(monkeypatch):
    """装了 plyer 就直接用 plyer。"""
    seen = {}

    def fake_notify(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(channels, "detect_platform", lambda: "unknown")
    monkeypatch.setattr(channels, "load_plyer_notifier", lambda: fake_notify)

    assert DesktopChannel(title="T").send(ALERT) is True
    assert seen["title"] == "T"
    assert "rm -rf /tmp/x" in seen["message"]


def test_desktop_channel_on_windows_passes_message_via_env(monkeypatch):
    """Windows 下告警正文必须走环境变量，不能拼进命令行。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "windows")
    monkeypatch.setattr(channels, "load_plyer_notifier", lambda: None)

    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakePopen(args)

    monkeypatch.setattr(channels.subprocess, "Popen", fake_popen)

    channel = DesktopChannel(title="哨兵告警")
    try:
        assert channel.send(ALERT) is True
    finally:
        channel.close()

    args = list(captured["args"])
    joined = " ".join(args)
    # 命令行里绝不能出现正文 —— 那样等于给日志内容开了注入通道。
    assert "rm -rf /tmp/x" not in joined
    assert "$env:SENTINEL_ALERT_BODY" in joined
    assert "$env:SENTINEL_ALERT_TITLE" in joined

    env = captured["kwargs"]["env"]
    assert "rm -rf /tmp/x" in env["SENTINEL_ALERT_BODY"]
    assert env["SENTINEL_ALERT_TITLE"] == "哨兵告警"
    # 隐藏窗口，避免弹黑框。
    assert captured["kwargs"]["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )


def test_desktop_channel_never_raises_when_process_fails(monkeypatch):
    """拉不起子进程时返回 False，不抛异常。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "windows")
    monkeypatch.setattr(channels, "load_plyer_notifier", lambda: None)

    def boom(*args, **kwargs):
        raise OSError("找不到 powershell")

    monkeypatch.setattr(channels.subprocess, "Popen", boom)
    assert DesktopChannel().send(ALERT) is False


# ---------------------------------------------------------------------------
# 声音通道
# ---------------------------------------------------------------------------


def test_sound_channel_returns_false_without_mechanism(monkeypatch):
    """平台没有提示音方式时返回 False，不抛异常。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "unknown")
    assert SoundChannel().send(ALERT) is False


def test_sound_channel_on_windows_uses_winsound(monkeypatch):
    """Windows 走标准库 winsound，不需要子进程。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "windows")
    fake = _FakeWinsound()
    monkeypatch.setattr(channels, "load_winsound", lambda: fake)

    assert SoundChannel().send(ALERT) is True
    assert len(fake.beeps) == 1
    frequency, duration = fake.beeps[0]
    assert 37 <= frequency <= 32767
    assert 200 <= duration <= 400


def test_sound_channel_returns_false_when_winsound_missing(monkeypatch):
    """winsound 不可用时返回 False，不抛异常。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "windows")
    monkeypatch.setattr(channels, "load_winsound", lambda: None)
    assert SoundChannel().send(ALERT) is False


def test_sound_channel_never_raises_when_beep_fails(monkeypatch):
    """蜂鸣失败时返回 False，不抛异常。"""
    monkeypatch.setattr(channels, "detect_platform", lambda: "windows")
    fake = _FakeWinsound(error=RuntimeError("声卡炸了"))
    monkeypatch.setattr(channels, "load_winsound", lambda: fake)
    assert SoundChannel().send(ALERT) is False


# ---------------------------------------------------------------------------
# Webhook 通道
# ---------------------------------------------------------------------------


def test_webhook_builds_dingtalk_payload():
    """钉钉报文格式。"""
    channel = WebhookChannel(
        url="https://example.invalid/hook", format="dingtalk"
    )
    payload = channel.build_payload(ALERT)
    assert payload["msgtype"] == "text"
    assert "rm -rf /tmp/x" in payload["text"]["content"]


def test_webhook_builds_feishu_payload():
    """飞书报文格式。"""
    channel = WebhookChannel(
        url="https://example.invalid/hook", format="feishu"
    )
    payload = channel.build_payload(ALERT)
    assert payload["msg_type"] == "text"
    assert "rm -rf /tmp/x" in payload["content"]["text"]


def test_webhook_builds_generic_payload():
    """通用报文带完整告警对象。"""
    channel = WebhookChannel(
        url="https://example.invalid/hook", format="generic"
    )
    payload = channel.build_payload(ALERT)
    assert payload["text"].startswith("[CRITICAL] DANGEROUS")
    assert payload["alert"]["verdict"] == "DANGEROUS"
    assert payload["alert"]["command"] == "rm -rf /tmp/x"


def test_webhook_refuses_empty_url(monkeypatch):
    """地址为空时直接放弃，不发请求。"""
    fake = _FakeRequests()
    monkeypatch.setattr(channels, "requests", fake)
    channel = WebhookChannel(url="")
    assert channel.send(ALERT) is False
    assert fake.calls == []


def test_webhook_refuses_non_http_scheme(monkeypatch):
    """非 http/https 的地址一律拒绝。"""
    fake = _FakeRequests()
    monkeypatch.setattr(channels, "requests", fake)
    for url in ("file:///tmp/x", "ftp://example.invalid/hook", "gopher://x"):
        channel = WebhookChannel(url=url)
        assert channel.send(ALERT) is False
    assert fake.calls == []


def test_webhook_posts_payload(monkeypatch):
    """正常投递：报文、超时都对得上。

    ⚠️ v1.1.0 起 Webhook 走**后台线程**投递 —— ``send()`` 只入队，
    真正的 HTTP 在后台做。所以断言前必须 ``flush()`` 等它投完。
    """
    fake = _FakeRequests(status_code=200)
    monkeypatch.setattr(channels, "requests", fake)
    channel = WebhookChannel(
        url="https://example.invalid/hook", timeout=7, format="dingtalk"
    )
    assert channel.send(ALERT) is True
    assert channel.flush(timeout=5.0) is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "https://example.invalid/hook"
    assert fake.calls[0]["timeout"] == 7
    assert fake.calls[0]["json"]["msgtype"] == "text"
    assert channel.last_delivered is True
    assert channel.failed_sends == 0


def test_webhook_non_2xx_delivery_fails(monkeypatch):
    """非 2xx 响应：投递最终失败，并留下错误说明。

    ⚠️ ``send()`` 返回的是「**已入队**」，不是「已送达」——
    送达与否要看 ``last_delivered`` / ``last_error``。
    5xx 属于可重试故障，会重试 ``max_retries`` 次后才放弃。
    """
    fake = _FakeRequests(status_code=500)
    monkeypatch.setattr(channels, "requests", fake)
    channel = WebhookChannel(
        url="https://example.invalid/hook", max_retries=1, retry_backoff=0.01
    )
    assert channel.send(ALERT) is True
    assert channel.flush(timeout=10.0) is True
    assert channel.last_delivered is False
    assert "500" in channel.last_error
    assert channel.failed_sends == 1


def test_webhook_network_error_delivery_fails(monkeypatch):
    """网络异常：投递失败但不抛异常（后台线程兜住）。"""
    fake = _FakeRequests(error=OSError("连接失败"))
    monkeypatch.setattr(channels, "requests", fake)
    channel = WebhookChannel(
        url="https://example.invalid/hook", max_retries=1, retry_backoff=0.01
    )
    assert channel.send(ALERT) is True
    assert channel.flush(timeout=10.0) is True
    assert channel.last_delivered is False
    assert "连接失败" in channel.last_error
    assert channel.failed_sends == 1
