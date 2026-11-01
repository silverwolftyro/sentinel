"""告警合并推送测试（v2.0.0）。

覆盖：同指纹合并、红线直通、记录型通道不合并、合并计数附加到下一次推送。
"""

from __future__ import annotations

from src.core.channels import BaseChannel
from src.core.notifier import AlertCoalescer, Notifier


class _RecordingChannel(BaseChannel):
    """记录收到的告警的假通道。"""

    name = "recording"

    def __init__(self, notification: bool = False) -> None:
        self.received = []
        self.is_notification = notification

    def send(self, alert):
        """把告警记下来。"""
        self.received.append(dict(alert))
        return True


def _alert(verdict="LOOP", rule_id="context_stuck", command="pytest"):
    """造一条告警。"""
    return {
        "verdict": verdict,
        "level": "warn",
        "rule_id": rule_id,
        "command": command,
        "agent": "codex",
        "reason": "测试连续失败3次",
    }


# ---------------------------------------------------------------------------
# 合并器本身
# ---------------------------------------------------------------------------
def test_same_fingerprint_is_merged():
    """同一件事在窗口内只推一次。"""
    coalescer = AlertCoalescer(window_seconds=60)

    assert coalescer.decide(_alert()) == ("send", 0)
    assert coalescer.decide(_alert()) == ("merge", 0)
    assert coalescer.decide(_alert()) == ("merge", 0)


def test_merged_count_is_attached_to_next_push():
    """窗口过后再来的那条推送，要带上之前合并掉的条数。"""
    import time

    coalescer = AlertCoalescer(window_seconds=0.05)

    coalescer.decide(_alert())
    coalescer.decide(_alert())
    coalescer.decide(_alert())
    time.sleep(0.08)

    decision, merged = coalescer.decide(_alert())
    assert decision == "send"
    assert merged == 2


def test_expired_summary_is_produced_when_burst_ends():
    """连续告警**停了**时，摘要也要补出来 —— 合并不是隐瞒。"""
    import time

    coalescer = AlertCoalescer(window_seconds=0.05)
    coalescer.decide(_alert())
    coalescer.decide(_alert())
    coalescer.decide(_alert())
    time.sleep(0.08)

    summaries = coalescer.expired_summaries()

    assert len(summaries) == 1
    assert summaries[0]["merged_count"] == 2
    assert summaries[0]["merged_summary"] is True
    assert "合并了 2 条" in summaries[0]["reason"]
    # 取过一次就没了 —— 不能重复报。
    assert coalescer.expired_summaries() == []


def test_different_command_is_not_merged():
    """命令不同就是**另一件事**，不该被合并掉。"""
    coalescer = AlertCoalescer(window_seconds=60)

    coalescer.decide(_alert(command="pytest a"))
    decision, _ = coalescer.decide(_alert(command="pytest b"))

    assert decision == "send"


def test_red_line_always_passes_through():
    """红线级告警永远直接推 —— 合并不能盖住要紧的事。"""
    coalescer = AlertCoalescer(window_seconds=60)

    assert coalescer.decide(_alert(verdict="DANGEROUS")) == ("send", 0)
    assert coalescer.decide(_alert(verdict="DANGEROUS")) == ("send", 0)
    assert coalescer.decide(_alert(verdict="DANGEROUS")) == ("send", 0)


def test_window_zero_disables_merging():
    """窗口设成 0 就等于关掉合并。"""
    coalescer = AlertCoalescer(window_seconds=0)

    assert coalescer.decide(_alert()) == ("send", 0)
    assert coalescer.decide(_alert()) == ("send", 0)


# ---------------------------------------------------------------------------
# 分发器
# ---------------------------------------------------------------------------
def _notifier(channels):
    """造一个只带指定通道的分发器。"""
    notifier = Notifier({"alert": {"enabled": True}})
    notifier.channels = list(channels)
    return notifier


def test_notification_channel_merges_repeats():
    """通知型通道：5 条一样的告警只敲门 1 次。"""
    channel = _RecordingChannel(notification=True)
    notifier = _notifier([channel])

    for _ in range(5):
        notifier.notify(_alert())

    assert len(channel.received) == 1


def test_record_channel_keeps_every_alert():
    """记录型通道是流水账，一条都不能少。"""
    channel = _RecordingChannel(notification=False)
    notifier = _notifier([channel])

    for _ in range(5):
        notifier.notify(_alert())

    assert len(channel.received) == 5


def test_red_line_reaches_notification_channel_every_time():
    """红线级必须每次都推到 —— 半夜的安全阀不能被合并掉。"""
    channel = _RecordingChannel(notification=True)
    notifier = _notifier([channel])

    for _ in range(3):
        notifier.notify(_alert(verdict="DANGEROUS"))

    assert len(channel.received) == 3


def test_merged_count_reaches_the_channel():
    """被合并的条数要附在下一次推送上。"""
    import time

    channel = _RecordingChannel(notification=True)
    notifier = _notifier([channel])
    notifier.merge_seconds = 0.05
    notifier.coalescer = AlertCoalescer(window_seconds=0.05)

    notifier.notify(_alert())
    notifier.notify(_alert())
    notifier.notify(_alert())
    time.sleep(0.08)
    notifier.notify(_alert())

    # 合并掉的 2 条必须以某种形式送到用户面前 ——
    # 要么附在下一次推送上，要么作为一条摘要补推。
    counts = [item.get("merged_count", 0) for item in channel.received]
    assert 2 in counts


def test_merged_count_shows_in_human_text():
    """合并提示要出现在给人看的正文里。"""
    from src.core.channels import format_alert

    text = format_alert({**_alert(), "merged_count": 3})

    assert "另有 3 条同类告警已合并" in text
