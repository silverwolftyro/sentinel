"""告警分发层测试。

全部离线：通道一律用假对象替换，不会触发真实弹窗 / 声音 / 网络请求。
"""

from __future__ import annotations

import json
import logging

from src.core.channels import BaseChannel
from src.core.notifier import Notifier

#: 一条典型的 DANGEROUS 告警（形状来自 ``SentinelEngine._build_alert``）。
ALERT = {
    "timestamp": 1700000000.0,
    "verdict": "DANGEROUS",
    "level": "critical",
    "rule_id": "dangerous_command",
    "reason": "匹配到高危命令: rm -rf",
    "tool": "Bash",
    "command": "rm -rf /tmp/x",
    "explanation": "清理临时构建产物。",
    "explanation_degraded": False,
}


# ---------------------------------------------------------------------------
# 假通道
# ---------------------------------------------------------------------------


class _Recorder(BaseChannel):
    """记录收到告警的假通道。"""

    name = "recorder"

    def __init__(self):
        self.alerts = []
        self.closed = False

    def send(self, alert):
        self.alerts.append(alert)
        return True

    def close(self):
        self.closed = True


class _Boom(BaseChannel):
    """总是抛异常的假通道。"""

    name = "boom"

    def __init__(self):
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        raise RuntimeError("通道炸了")

    def close(self):
        return None


class _Failing(BaseChannel):
    """总是返回 False 的假通道。"""

    name = "failing"

    def __init__(self):
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        return False

    def close(self):
        return None


def _notifier(**alert_config):
    """构造一个不带通道的 Notifier（测试里自行注入假通道）。"""
    base = {"enabled": True, "min_level": "info", "channels": []}
    base.update(alert_config)
    return Notifier({"alert": base})


# ---------------------------------------------------------------------------
# 等级过滤与总开关
# ---------------------------------------------------------------------------


def test_notifier_filters_by_min_level():
    """低于 min_level 的告警被丢弃，达到门槛的照常投递。"""
    notifier = _notifier(min_level="critical")
    recorder = _Recorder()
    notifier.channels = [recorder]

    notifier.notify({"level": "warn", "verdict": "LOOP", "reason": "疑似死循环"})
    assert recorder.alerts == []

    notifier.notify({"level": "critical", "verdict": "DANGEROUS"})
    assert len(recorder.alerts) == 1


def test_notifier_passes_warn_at_warn_threshold():
    """门槛就是 warn 时，warn 告警应当放行。"""
    notifier = _notifier(min_level="warn")
    recorder = _Recorder()
    notifier.channels = [recorder]
    notifier.notify({"level": "warn", "verdict": "LOOP"})
    assert len(recorder.alerts) == 1


def test_notifier_unknown_level_is_not_dropped():
    """无法识别的等级按最高级处理，不被 min_level 悄悄吃掉。"""
    notifier = _notifier(min_level="critical")
    recorder = _Recorder()
    notifier.channels = [recorder]
    notifier.notify({"level": "catastrophic", "verdict": "DANGEROUS"})
    assert len(recorder.alerts) == 1


def test_notifier_disabled_does_nothing():
    """总开关关闭时什么都不做。"""
    notifier = _notifier(enabled=False, min_level="info")
    recorder = _Recorder()
    notifier.channels = [recorder]
    notifier.notify(ALERT)
    assert recorder.alerts == []


# ---------------------------------------------------------------------------
# 失败隔离
# ---------------------------------------------------------------------------


def test_notifier_isolates_channel_exception():
    """一个通道抛异常，其余通道照常收到告警，notify() 本身不抛。"""
    notifier = _notifier()
    boom = _Boom()
    recorder = _Recorder()
    notifier.channels = [boom, recorder]

    notifier.notify(ALERT)

    assert boom.calls == 1
    assert len(recorder.alerts) == 1


def test_notifier_isolates_channel_failure():
    """一个通道返回 False，其余通道照常收到告警。"""
    notifier = _notifier()
    failing = _Failing()
    recorder = _Recorder()
    notifier.channels = [failing, recorder]

    notifier.notify(ALERT)

    assert failing.calls == 1
    assert len(recorder.alerts) == 1


def test_notifier_notify_never_raises_on_bad_alert():
    """畸形告警（甚至不是字典）也不能让 notify() 抛异常。"""
    notifier = _notifier()
    recorder = _Recorder()
    notifier.channels = [recorder]
    notifier.notify("不是字典")
    assert len(recorder.alerts) == 1


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def test_notifier_builds_channels_from_config(tmp_path):
    """按配置装配通道；未启用的与未知的类型都被跳过。"""
    notifier = Notifier(
        {
            "alert": {
                "enabled": True,
                "channels": [
                    {"type": "console", "enabled": True},
                    {
                        "type": "file",
                        "enabled": True,
                        "path": str(tmp_path / "alerts.jsonl"),
                    },
                    {"type": "sound", "enabled": False},
                    {"type": "smoke-signal", "enabled": True},
                ],
            }
        }
    )
    try:
        assert [c.name for c in notifier.channels] == ["console", "file"]
    finally:
        notifier.close()


def test_notifier_skips_unknown_channel_type(caplog):
    """未知通道类型只记 warning 并跳过，不抛异常。"""
    with caplog.at_level(logging.WARNING, logger="src.core.notifier"):
        notifier = Notifier(
            {
                "alert": {
                    "enabled": True,
                    "channels": [{"type": "smoke-signal", "enabled": True}],
                }
            }
        )
    try:
        assert notifier.channels == []
        messages = [record.getMessage() for record in caplog.records]
        assert any("smoke-signal" in message for message in messages)
    finally:
        notifier.close()


def test_notifier_reads_config_from_path(tmp_path):
    """只给 config_path 时也要能把配置读出来。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "alert:\n"
        "  enabled: true\n"
        "  min_level: critical\n"
        "  channels:\n"
        "    - type: console\n"
        "      enabled: true\n",
        encoding="utf-8",
    )
    notifier = Notifier(config_path=str(config_path))
    try:
        assert notifier.min_level == "critical"
        assert [c.name for c in notifier.channels] == ["console"]
    finally:
        notifier.close()


def test_notifier_default_min_level():
    """没写 min_level 时按 info 处理（不过滤）。"""
    notifier = Notifier({"alert": {"enabled": True, "channels": []}})
    assert notifier.min_level == "info"


# ---------------------------------------------------------------------------
# 端到端（仍然离线）
# ---------------------------------------------------------------------------


def test_notifier_writes_file_channel(tmp_path):
    """真实文件通道：告警落到 JSONL 文件里。"""
    path = tmp_path / "logs" / "alerts.jsonl"
    notifier = Notifier(
        {
            "alert": {
                "enabled": True,
                "min_level": "warn",
                "channels": [
                    {"type": "file", "enabled": True, "path": str(path)},
                ],
            }
        }
    )
    try:
        notifier.notify(ALERT)
    finally:
        notifier.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["verdict"] == "DANGEROUS"


def test_notifier_close_closes_channels():
    """close() 会关闭所有通道。"""
    notifier = _notifier()
    recorder = _Recorder()
    notifier.channels = [recorder]
    notifier.close()
    assert recorder.closed is True


def test_notifier_context_manager_closes():
    """with 语句退出时会关闭通道。"""
    recorder = _Recorder()
    with _notifier() as notifier:
        notifier.channels = [recorder]
    assert recorder.closed is True
