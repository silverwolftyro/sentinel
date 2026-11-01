"""微观统计学检测器测试 —— 「零延迟、零误报」的对抗性验证。

覆盖三族判定：

1. **高频重复**（``STATS_LOOP``）—— 同文件 / 同命令的窗口内频次；
2. **无效编辑**（``STATS_NOOP``）—— 改了等于没改；
3. **极端摸鱼**（``STATS_STALL``）—— 两次动作间隔太久。

外加引擎接口适配（``MicroStatsReporter.attach``）与橘黄流水行。

⚠️ 本文件刻意塞了大量**反向用例**（本该不报的场景）——
   一个只会喊「狼来了」的检测器没有意义。
   每条正向用例都配一条「差一步就不该报」的反向用例。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.core.micro_stats import (
    ONTRACK_MARKER,
    TAG_INVALID_EDIT,
    TAG_STALL,
    TAG_STATS_LOOP,
    VERDICT_STATS_LOOP,
    VERDICT_STATS_NOOP,
    VERDICT_STATS_STALL,
    MicroStatsDetector,
    MicroStatsReporter,
    build_micro_stats,
    finding_to_verdict,
)

#: 基准时刻（固定值 —— 测试绝不能依赖真实时间）。
BASE_TS = 1_700_000_000.0


class _Clock:
    """可控时钟 —— 让窗口行为完全确定。"""

    def __init__(self, start: float = BASE_TS) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """把时钟往前拨。"""
        self.now += float(seconds)


def tool_event(
    name: str,
    arguments: Dict[str, Any],
    ts: Optional[float] = None,
    call_id: str = "",
) -> Dict[str, Any]:
    """构造一个 ``tool_call`` 事件。"""
    command = arguments.get("command", "")
    event: Dict[str, Any] = {
        "source": "mcode",
        "type": "tool_call",
        "session_id": None,
        "timestamp": ts,
        "tool": {
            "name": name,
            "arguments": arguments,
            "command": command,
        },
        "text": "",
        "raw": {},
    }
    if call_id:
        event["tool_use_id"] = call_id
    return event


def thinking_event(
    text: str = "思考中",
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """构造一个 ``thinking`` 事件。"""
    return {
        "source": "mcode",
        "type": "thinking",
        "session_id": None,
        "timestamp": ts,
        "tool": None,
        "text": text,
        "raw": {},
    }


def tool_result_event(
    text: str,
    ts: Optional[float] = None,
    call_id: str = "",
    is_error: bool = False,
) -> Dict[str, Any]:
    """构造一个 ``tool_result`` 事件。"""
    event: Dict[str, Any] = {
        "source": "mcode",
        "type": "tool_result",
        "session_id": None,
        "timestamp": ts,
        "tool": None,
        "text": text,
        "is_error": is_error,
        "raw": {},
    }
    if call_id:
        event["tool_use_id"] = call_id
    return event


def verdicts(findings: List[Any]) -> List[str]:
    """取出一批结论的判定名。"""
    return [item.verdict for item in findings]


def make_detector(
    clock: Optional[_Clock] = None,
    **kwargs: Any,
) -> MicroStatsDetector:
    """造一个检测器（默认挂可控时钟）。"""
    return MicroStatsDetector(clock=clock or _Clock(), **kwargs)


# ---------------------------------------------------------------------------
# 高频重复：同一文件
# ---------------------------------------------------------------------------
def test_six_reads_of_same_file_fire_on_the_sixth():
    """连续 6 次读同一个文件 —— 第 6 次**当场**报警。"""
    detector = make_detector()
    hits: List[Any] = []
    for index in range(6):
        event = tool_event(
            "Read",
            {"file_path": "src/core/engine.py"},
            ts=BASE_TS + index * 10,
        )
        findings = detector.observe(event)
        if index < 5:
            # 前 5 次必须**一声不吭** —— 零误报。
            assert findings == [], "第 {} 次不该报警".format(index + 1)
        else:
            hits = findings

    assert len(hits) == 1
    assert hits[0].verdict == VERDICT_STATS_LOOP
    assert hits[0].tag == TAG_STATS_LOOP
    assert hits[0].detail["count"] == 6
    assert hits[0].detail["reads"] == 6
    assert "src/core/engine.py" in hits[0].reason


def test_five_reads_of_same_file_never_fire():
    """5 次读同一个文件 —— 不到阈值，**绝不**报警。"""
    detector = make_detector()
    for index in range(5):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "src/core/engine.py"},
                ts=BASE_TS + index * 10,
            )
        )
        assert findings == []


def test_six_edits_of_same_file_fire():
    """连续 6 次改同一个文件 —— 同样算高频重复。"""
    detector = make_detector()
    for index in range(6):
        findings = detector.observe(
            tool_event(
                "Edit",
                {
                    "file_path": "src/core/engine.py",
                    "old_string": "a{}".format(index),
                    "new_string": "b{}".format(index),
                },
                ts=BASE_TS + index * 10,
            )
        )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_LOOP
    assert findings[0].detail["edits"] == 6


def test_reads_and_edits_share_the_same_bucket():
    """读与改是**同一个桶** —— 3 读 + 3 改也算 6 次。"""
    detector = make_detector()
    findings: List[Any] = []
    for index in range(3):
        detector.observe(
            tool_event(
                "Read",
                {"file_path": "a.py"},
                ts=BASE_TS + index * 10,
            )
        )
    for index in range(3):
        findings = detector.observe(
            tool_event(
                "Edit",
                {
                    "file_path": "a.py",
                    "old_string": "x{}".format(index),
                    "new_string": "y{}".format(index),
                },
                ts=BASE_TS + 100 + index * 10,
            )
        )
    assert len(findings) == 1
    assert findings[0].detail["count"] == 6
    assert findings[0].detail["reads"] == 3
    assert findings[0].detail["edits"] == 3


def test_reading_different_files_never_fires():
    """读 6 个**不同**的文件 —— 一条都不该报。"""
    detector = make_detector()
    for index in range(6):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "src/mod{}.py".format(index)},
                ts=BASE_TS + index * 10,
            )
        )
        assert findings == []


def test_reads_outside_the_window_never_fire():
    """6 次读同一个文件，但**每次间隔 11 分钟** —— 窗口外不算数。"""
    detector = make_detector()
    for index in range(6):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "src/core/engine.py"},
                ts=BASE_TS + index * 11 * 60,
            )
        )
        assert findings == []


def test_seventh_read_does_not_repeat_the_alarm():
    """第 7 次读同一个文件 —— **不重复刷屏**。"""
    detector = make_detector()
    fired = 0
    for index in range(9):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "src/core/engine.py"},
                ts=BASE_TS + index * 10,
            )
        )
        if findings:
            fired += 1
    assert fired == 1


def test_loop_alarm_rearms_after_the_window_drops():
    """窗口滑过去之后重新武装 —— 下一波还能报。"""
    detector = make_detector()
    fired = 0
    for index in range(6):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "src/core/engine.py"},
                ts=BASE_TS + index * 10,
            )
        )
        if any(item.verdict == VERDICT_STATS_LOOP for item in findings):
            fired += 1
    # 再隔 20 分钟重来一波（跨过窗口）。
    for index in range(6):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "src/core/engine.py"},
                ts=BASE_TS + 20 * 60 + index * 10,
            )
        )
        if any(item.verdict == VERDICT_STATS_LOOP for item in findings):
            fired += 1
    assert fired == 2


# ---------------------------------------------------------------------------
# 高频重复：同一条命令
# ---------------------------------------------------------------------------
def test_four_identical_bash_commands_fire_on_the_fourth():
    """连续 4 次执行 ``pytest -q`` —— 第 4 次**当场**报警。"""
    detector = make_detector()
    for index in range(4):
        findings = detector.observe(
            tool_event(
                "Bash",
                {"command": "pytest -q"},
                ts=BASE_TS + index * 30,
            )
        )
        if index < 3:
            assert findings == [], "第 {} 次不该报警".format(index + 1)
        else:
            assert len(findings) == 1
            assert findings[0].verdict == VERDICT_STATS_LOOP
            assert findings[0].kind == "command_repeat"
            assert findings[0].detail["count"] == 4
            assert "pytest -q" in findings[0].reason


def test_three_identical_bash_commands_never_fire():
    """3 次执行同一条命令 —— 不到阈值，**绝不**报警。"""
    detector = make_detector()
    for index in range(3):
        findings = detector.observe(
            tool_event(
                "Bash",
                {"command": "pytest -q"},
                ts=BASE_TS + index * 30,
            )
        )
        assert findings == []


def test_different_commands_never_fire():
    """4 条**不同**的命令 —— 一条都不该报。"""
    detector = make_detector()
    for index in range(4):
        findings = detector.observe(
            tool_event(
                "Bash",
                {"command": "pytest -q tests/test_{}.py".format(index)},
                ts=BASE_TS + index * 30,
            )
        )
        assert findings == []


def test_same_command_outside_the_window_never_fires():
    """同一条命令每 11 分钟一次 —— 窗口外不算数。"""
    detector = make_detector()
    for index in range(4):
        findings = detector.observe(
            tool_event(
                "Bash",
                {"command": "pytest -q"},
                ts=BASE_TS + index * 11 * 60,
            )
        )
        assert findings == []


def test_command_spacing_variants_are_the_same_command():
    """``pytest  -q`` 与 ``pytest -q`` —— 算**同一条**命令。"""
    detector = make_detector()
    commands = ["pytest -q", "pytest  -q", "PYTEST -Q", "pytest -q"]
    findings: List[Any] = []
    for index, command in enumerate(commands):
        findings = detector.observe(
            tool_event("Bash", {"command": command}, ts=BASE_TS + index * 30)
        )
    assert len(findings) == 1
    assert findings[0].kind == "command_repeat"


def test_read_tool_is_not_counted_as_a_command():
    """``Read`` 不该被当成命令 —— 命令桶只认命令类工具。"""
    detector = make_detector()
    for index in range(8):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "a.py"},
                ts=BASE_TS + index * 10,
            )
        )
        assert all(item.kind != "command_repeat" for item in findings)


# ---------------------------------------------------------------------------
# 无效编辑
# ---------------------------------------------------------------------------
def test_identical_edit_is_flagged_as_invalid():
    """改前改后**一模一样** —— 打上 ``[无效编辑]``。"""
    detector = make_detector()
    findings = detector.observe(
        tool_event(
            "Edit",
            {
                "file_path": "src/core/engine.py",
                "old_string": "return None",
                "new_string": "return None",
            },
            ts=BASE_TS,
        )
    )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_NOOP
    assert findings[0].tag == TAG_INVALID_EDIT
    assert findings[0].detail["change"] == "identical"
    assert findings[0].detail["old_length"] == findings[0].detail["new_length"]


def test_whitespace_only_edit_is_flagged_as_invalid():
    """只删了空格 / 换行 —— 也算 ``[无效编辑]``。"""
    detector = make_detector()
    findings = detector.observe(
        tool_event(
            "Edit",
            {
                "file_path": "src/core/engine.py",
                "old_string": "def foo():\n    return 1\n",
                "new_string": "def foo():\n        return 1",
            },
            ts=BASE_TS,
        )
    )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_NOOP
    assert findings[0].detail["change"] == "whitespace_only"


def test_substantive_edit_is_not_flagged():
    """真改了东西 —— **绝不**误报。"""
    detector = make_detector()
    findings = detector.observe(
        tool_event(
            "Edit",
            {
                "file_path": "src/core/engine.py",
                "old_string": "return None",
                "new_string": "return {}",
            },
            ts=BASE_TS,
        )
    )
    assert findings == []


def test_edit_without_evidence_is_not_guessed():
    """拿不到前后文本 —— **不下结论**（宁可漏报，不误报）。"""
    detector = make_detector()
    findings = detector.observe(
        tool_event(
            "Edit",
            {"file_path": "src/core/engine.py"},
            ts=BASE_TS,
        )
    )
    assert findings == []


def test_write_with_identical_content_is_flagged_by_fingerprint():
    """写回去的内容和刚读到的一模一样 —— 靠**文件指纹**抓住。"""
    detector = make_detector()
    content = "print('hello')\n"
    detector.observe(
        tool_event(
            "Read",
            {"file_path": "hello.py"},
            ts=BASE_TS,
            call_id="call_1",
        )
    )
    detector.observe(
        tool_result_event(content, ts=BASE_TS + 1, call_id="call_1")
    )
    findings = detector.observe(
        tool_event(
            "Write",
            {"file_path": "hello.py", "content": content},
            ts=BASE_TS + 2,
        )
    )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_NOOP
    assert findings[0].detail["source"] == "fingerprint"
    assert findings[0].detail["change"] == "identical"


def test_command_output_is_never_treated_as_file_content():
    """命令回执**不是**文件内容 —— 绝不能拿去比指纹。"""
    detector = make_detector()
    detector.observe(
        tool_event("Bash", {"command": "cat hello.py"}, ts=BASE_TS)
    )
    detector.observe(tool_result_event("hello.py 内容", ts=BASE_TS + 1))
    findings = detector.observe(
        tool_event(
            "Write",
            {"file_path": "hello.py", "content": "hello.py 内容"},
            ts=BASE_TS + 2,
        )
    )
    assert findings == []


def test_write_with_different_content_is_not_flagged():
    """写的是**新内容** —— 不该报。"""
    detector = make_detector()
    content = "print('hello')\n"
    detector.observe(
        tool_event("Read", {"file_path": "hello.py"}, ts=BASE_TS)
    )
    detector.observe(tool_result_event(content, ts=BASE_TS + 1))
    findings = detector.observe(
        tool_event(
            "Write",
            {"file_path": "hello.py", "content": "print('bye')\n"},
            ts=BASE_TS + 2,
        )
    )
    assert findings == []


# ---------------------------------------------------------------------------
# 极端摸鱼（疑似卡死）
# ---------------------------------------------------------------------------
def test_long_gap_between_thinking_and_tool_call_is_flagged():
    """思考完隔了 16 分钟才动手 —— 打上 ``[疑似卡死]``。"""
    detector = make_detector()
    detector.observe(thinking_event(ts=BASE_TS))
    findings = detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS + 16 * 60)
    )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_STALL
    assert findings[0].tag == TAG_STALL
    assert findings[0].detail["gap_minutes"] == 16.0


def test_gap_below_threshold_is_not_flagged():
    """隔了 14 分钟 —— 不到阈值，**绝不**报警。"""
    detector = make_detector()
    detector.observe(thinking_event(ts=BASE_TS))
    findings = detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS + 14 * 60)
    )
    assert findings == []


def test_first_event_is_never_flagged_as_stalled():
    """第一个事件没有「上一个」，**不该**报。"""
    detector = make_detector()
    findings = detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS)
    )
    assert all(item.verdict != VERDICT_STATS_STALL for item in findings)


def test_text_event_does_not_reset_the_stall_timer():
    """``text`` 事件**不重置**计时器 —— 间隔仍按上次动作算。"""
    detector = make_detector()
    detector.observe(thinking_event(ts=BASE_TS))
    detector.observe(
        {
            "type": "text",
            "text": "中间插一句",
            "timestamp": BASE_TS + 8 * 60,
        }
    )
    findings = detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS + 16 * 60)
    )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_STALL


def test_tool_call_gap_is_measured_between_calls():
    """两次工具调用之间隔太久 —— 同样该报。"""
    detector = make_detector()
    detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS)
    )
    findings = detector.observe(
        tool_event("Read", {"file_path": "b.py"}, ts=BASE_TS + 20 * 60)
    )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_STALL


# ---------------------------------------------------------------------------
# 鲁棒性
# ---------------------------------------------------------------------------
def test_malformed_events_never_raise():
    """畸形输入 —— **绝不**抛异常（红线 R5）。"""
    detector = make_detector()
    for bad in (None, [], "字符串", 42, {}, {"type": None}):
        assert detector.observe(bad) == []  # type: ignore[arg-type]


def test_disabled_detector_returns_nothing():
    """关掉的检测器 —— 一声不吭。"""
    detector = MicroStatsDetector(enabled=False, clock=_Clock())
    for index in range(10):
        findings = detector.observe(
            tool_event(
                "Read",
                {"file_path": "a.py"},
                ts=BASE_TS + index,
            )
        )
        assert findings == []


def test_reset_clears_all_state():
    """复位 —— 状态归零。"""
    detector = make_detector()
    for index in range(6):
        detector.observe(
            tool_event(
                "Read",
                {"file_path": "a.py"},
                ts=BASE_TS + index,
            )
        )
    detector.reset()
    findings = detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS + 10)
    )
    assert findings == []


def test_events_without_timestamp_fall_back_to_the_clock():
    """事件没有时间戳 —— 退回墙上时钟，不崩。"""
    clock = _Clock()
    detector = make_detector(clock=clock)
    for index in range(6):
        clock.advance(10)
        findings = detector.observe(
            tool_event("Read", {"file_path": "a.py"}, ts=None)
        )
    assert len(findings) == 1
    assert findings[0].verdict == VERDICT_STATS_LOOP


def test_state_snapshot_is_readable():
    """状态快照 —— 给看板 / 调试用。"""
    detector = make_detector()
    detector.observe(
        tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS)
    )
    snapshot = detector.state()
    assert snapshot["enabled"] is True
    assert snapshot["window_minutes"] == 10.0
    assert snapshot["same_file_threshold"] == 5
    assert snapshot["files"][0]["path"] == "a.py"
    assert snapshot["files"][0]["count"] == 1


# ---------------------------------------------------------------------------
# 引擎接口
# ---------------------------------------------------------------------------
class _FakeEngine:
    """引擎替身 —— 只验证「包装」这件事。"""

    def __init__(self) -> None:
        self.logger = logging.getLogger("test-micro-stats-engine")
        self.seen: List[Dict[str, Any]] = []

    def process_event(self, event: Dict[str, Any]) -> None:
        """记录收到的事件。"""
        self.seen.append(event)


def test_attach_wraps_process_event_and_keeps_the_original_behaviour():
    """挂上去之后 —— 引擎的原有行为**一点不变**。"""
    engine = _FakeEngine()
    reporter = MicroStatsReporter(detector=make_detector())
    reporter.attach(engine)

    event = tool_event("Read", {"file_path": "a.py"}, ts=BASE_TS)
    engine.process_event(event)

    assert engine.seen == [event]


def test_attach_emits_the_orange_stream_line(caplog):
    """命中时 —— 写一行**橘黄**流水（``ONTRACK_JSON``）。"""
    engine = _FakeEngine()
    reporter = MicroStatsReporter(detector=make_detector())
    reporter.attach(engine)

    with caplog.at_level(logging.WARNING, logger="test-micro-stats-engine"):
        for index in range(6):
            engine.process_event(
                tool_event(
                    "Read",
                    {"file_path": "src/core/engine.py"},
                    ts=BASE_TS + index * 10,
                )
            )

    lines = [
        record.getMessage()
        for record in caplog.records
        if ONTRACK_MARKER in record.getMessage()
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].split(ONTRACK_MARKER, 1)[1].strip())
    assert payload["audit_level"] == "warn"
    assert payload["audit_text"].startswith("警告：")
    assert payload["micro_stats"]["verdict"] == VERDICT_STATS_LOOP
    assert payload["summary"]


def test_attach_is_idempotent():
    """重复挂载 —— 只挂一次（不能套娃）。"""
    engine = _FakeEngine()
    reporter = MicroStatsReporter(detector=make_detector())
    reporter.attach(engine)
    first = engine.process_event
    reporter.attach(engine)
    assert engine.process_event is first


def test_emit_writes_nothing_without_findings():
    """没有结论 —— 一行都不写。"""
    reporter = MicroStatsReporter(detector=make_detector())
    assert reporter.emit([], None) == 0


def test_finding_to_verdict_has_the_engine_shape():
    """结论 → 判定字典 —— 形状与既有检测器一致。"""
    detector = make_detector()
    findings = detector.observe(
        tool_event(
            "Edit",
            {
                "file_path": "a.py",
                "old_string": "x",
                "new_string": "x",
            },
            ts=BASE_TS,
        )
    )
    verdict = finding_to_verdict(findings[0])
    assert verdict["verdict"] == VERDICT_STATS_NOOP
    assert verdict["reason"].startswith(TAG_INVALID_EDIT)
    assert verdict["level"] == "warn"
    assert verdict["rule_id"] == "micro_stats.invalid_edit"


def test_observe_verdict_returns_the_first_finding():
    """单结论通道 —— 给引擎的单步入口用。"""
    detector = make_detector()
    verdict = detector.observe_verdict(
        tool_event(
            "Edit",
            {"file_path": "a.py", "old_string": "x", "new_string": "x"},
            ts=BASE_TS,
        )
    )
    assert verdict is not None
    assert verdict["verdict"] == VERDICT_STATS_NOOP
    assert detector.observe_verdict({"type": "text", "text": "hi"}) is None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_build_micro_stats_reads_the_config():
    """配置驱动 —— 阈值、窗口都能调。"""
    detector = build_micro_stats(
        {
            "rules": {
                "micro_stats": {
                    "enabled": True,
                    "window_minutes": 30,
                    "same_file_threshold": 2,
                    "same_command_threshold": 1,
                    "stall_minutes": 5,
                }
            }
        }
    )
    assert detector is not None
    assert detector.window_minutes == 30
    assert detector.same_file_threshold == 2
    assert detector.same_command_threshold == 1
    assert detector.stall_minutes == 5


def test_build_micro_stats_returns_none_when_disabled():
    """配置里关掉 —— 返回 ``None``（不挂载）。"""
    assert build_micro_stats({"rules": {"micro_stats": {"enabled": False}}}) is None
    assert build_micro_stats({}) is not None
