"""微观统计学异常检测（Micro-Stats Detector）验收演示。

这个脚本**完全不碰大模型**，只用纯 Python 统计器回放 100 步
「真实高频操作」，证明那些一眼可见的「沙雕」动作能在**进入大模型之前**
被拦下来。

跑法::

    python _构建与发布/scripts/demo_micro_stats.py
    python _构建与发布/scripts/demo_micro_stats.py --json 证据.json

场景编排（正好 100 步）::

    第  1 - 24 步   正常干活：读不同文件 / 改不同文件 / 跑不同命令
    第 25 - 31 步   连续 7 次 Read 同一个文件
                    → 第 30 步报警（第 6 次），第 31 步不重复刷屏
    第 32 - 36 步   正常干活（统计窗口开始往下滑）
    第 37 - 42 步   又一轮 6 次 Read 同一个文件
                    → 第 42 步二次报警（说明窗口滑过后重新武装了）
    第 43 - 46 步   无效编辑：完全没变 / 只差空白 / 有实质改动 / 拿不到证据
    第 47 - 48 步   极端摸鱼：间隔 16 分钟 → 第 47 步报警
    第 49 - 100 步  继续正常干活（验证零误报）

最后还会用**真实引擎**（``SentinelEngine``）跑一遍「同一条命令跑 4 次」，
证明告警行确实写进了看板流水区 —— 用看板**自己**的解析器验收。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adapters.base import BaseAdapter  # noqa: E402
from src.core.engine import SentinelEngine  # noqa: E402
from src.core.micro_stats import (  # noqa: E402
    ONTRACK_MARKER,
    TAG_INVALID_EDIT,
    TAG_STALL,
    TAG_STATS_LOOP,
    VERDICT_STATS_LOOP,
    VERDICT_STATS_NOOP,
    VERDICT_STATS_STALL,
    MicroStatsDetector,
    MicroStatsReporter,
)
from src.rules.basic import RuleEngine  # noqa: E402

#: 演示用的时间基准（固定值 —— 保证每次跑出来的结论完全一样）。
BASE_TS = 1_759_200_000.0

#: 正常阶段轮换的目标文件（8 个 —— 每个文件在 10 分钟窗口内都远够不到阈值）。
NORMAL_FILES = (
    "src/core/engine.py",
    "src/core/health.py",
    "src/core/live_audit.py",
    "src/core/timebox.py",
    "src/core/timeline.py",
    "src/rules/basic.py",
    "src/rules/registry.py",
    "src/gui/server.py",
)

#: 正常阶段轮换的命令（8 条 —— 各自都只跑 1~2 次，不会触发命令阈值）。
NORMAL_COMMANDS = (
    "pytest tests/test_engine_v33.py -q",
    "ruff check src/core/health.py",
    "pytest tests/test_live_audit.py -q",
    "git diff -- src/gui/server.py",
    "ruff check src/rules/basic.py",
    "pytest tests/test_timebox.py -q",
    "git status --short",
    "python -m ruff check src/core/timeline.py",
)

#: 「死磕」阶段死磕的那个文件（**不在**正常轮换表里，避免计数串味）。
STUCK_FILE = "src/core/micro_stats.py"

#: 「死磕」阶段死磕的那条命令（**不在**正常轮换表里）。
STUCK_COMMAND = "pytest -q"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _say(text: str = "") -> None:
    """打印一行（演示脚本用 print 是允许的）。

    :param text: 要打印的内容。
    :return: 无。
    """
    print(text, flush=True)


def _force_utf8_stdout() -> None:
    """把标准输出切到 UTF-8。

    ⚠️ Windows 控制台默认是 GBK —— 中文能打，
    但「🟠」「✅」这类符号会直接抛 ``UnicodeEncodeError``。
    这里统一改成 UTF-8 + ``replace``：最坏情况显示成 ``?``，
    绝不会因为一个符号把整个演示打断。

    :return: 无。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError) as exc:
            logging.getLogger(__name__).warning(
                "切换标准输出编码失败：%s: %s", type(exc).__name__, exc
            )


def _clock(ts: float) -> str:
    """把时间戳格式化成 ``HH:MM:SS``。

    :param ts: epoch 秒。
    :return: 时分秒文本。
    """
    offset = int(ts - BASE_TS)
    return "%02d:%02d:%02d" % (
        offset // 3600,
        (offset % 3600) // 60,
        offset % 60,
    )


def _thinking(ts: float, text: str) -> Dict[str, Any]:
    """造一个 ``thinking`` 事件。

    :param ts: 时间戳。
    :param text: 思考正文。
    :return: 事件字典。
    """
    return {
        "source": "demo",
        "type": "thinking",
        "session_id": "demo-micro-stats",
        "timestamp": ts,
        "text": text,
        "raw": {},
    }


def _tool_call(
    ts: float,
    name: str,
    arguments: Dict[str, Any],
    command: str,
    call_id: str,
) -> Dict[str, Any]:
    """造一个 ``tool_call`` 事件。

    :param ts: 时间戳。
    :param name: 工具名。
    :param arguments: 工具参数。
    :param command: 命令文本。
    :param call_id: 调用 ID。
    :return: 事件字典。
    """
    return {
        "source": "demo",
        "type": "tool_call",
        "session_id": "demo-micro-stats",
        "timestamp": ts,
        "tool_use_id": call_id,
        "tool": {
            "name": name,
            "arguments": arguments,
            "command": command,
        },
        "raw": {},
    }


def _tool_result(ts: float, text: str, call_id: str) -> Dict[str, Any]:
    """造一个 ``tool_result`` 事件。

    :param ts: 时间戳。
    :param text: 回执正文。
    :param call_id: 对应的调用 ID。
    :return: 事件字典。
    """
    return {
        "source": "demo",
        "type": "tool_result",
        "session_id": "demo-micro-stats",
        "timestamp": ts,
        "tool_use_id": call_id,
        "text": text,
        "is_error": False,
        "raw": {},
    }


@dataclass
class _Step:
    """演示里的一「步」。

    :param index: 步序（从 1 开始）。
    :param label: 这一步在干什么。
    :param events: 这一步产生的事件（按顺序喂给统计器）。
    :param expect: 这一步**应该**被拦截吗。
    :param expect_verdict: 期望的判定名。
    :param note: 补充说明。
    """

    index: int
    label: str
    events: List[Dict[str, Any]]
    expect: bool = False
    expect_verdict: str = ""
    note: str = ""


class _Timeline:
    """按时间顺序铺事件的小工具。

    :param base_ts: 时间基准。
    """

    def __init__(self, base_ts: float) -> None:
        self.base_ts = base_ts
        self.ts = base_ts
        self.steps: List[_Step] = []
        self._seq = 0

    def _next_id(self) -> str:
        """生成下一个调用 ID。

        :return: 调用 ID。
        """
        self._seq += 1
        return "call-%03d" % self._seq

    def add(
        self,
        label: str,
        *,
        tool: Optional[Tuple[str, Dict[str, Any], str]] = None,
        result: str = "",
        gap: float = 30.0,
        at: Optional[float] = None,
        expect: bool = False,
        expect_verdict: str = "",
        note: str = "",
    ) -> None:
        """铺一步（思考 + 工具调用 + 回执）。

        :param label: 这一步在干什么。
        :param tool: ``(工具名, 参数, 命令文本)``；不给就只铺一条思考。
        :param result: 工具回执正文。
        :param gap: 与上一步的时间间隔（秒）。
        :param at: 绝对时间戳；给了就用它（忽略 ``gap``）。
        :param expect: 这一步应该被拦截吗。
        :param expect_verdict: 期望的判定名。
        :param note: 补充说明。
        :return: 无。
        """
        if at is None:
            self.ts += gap
        else:
            self.ts = at
        ts = self.ts
        events: List[Dict[str, Any]] = [_thinking(ts, label)]
        if tool is not None:
            name, arguments, command = tool
            call_id = self._next_id()
            events.append(_tool_call(ts, name, arguments, command, call_id))
            events.append(_tool_result(ts, result, call_id))
        self.steps.append(
            _Step(
                index=len(self.steps) + 1,
                label=label,
                events=events,
                expect=expect,
                expect_verdict=expect_verdict,
                note=note,
            )
        )


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------
def _normal_step(
    line: _Timeline,
    slot: int,
    *,
    gap: float = 120.0,
) -> None:
    """铺一步「正常干活」。

    三种动作轮着来：读文件 / 跑命令 / 改文件。
    每个目标在 10 分钟窗口内都够不到阈值 —— 所以**不该**有任何告警。

    :param line: 时间线。
    :param slot: 轮换序号。
    :param gap: 与上一步的时间间隔（秒）。
    :return: 无。
    """
    kind = slot % 3
    target = slot // 3 % len(NORMAL_FILES)
    path = NORMAL_FILES[target]
    if kind == 0:
        line.add(
            "Read  %s" % path,
            tool=("Read", {"file_path": path}, ""),
            result="<文件正文 %d 行>" % (slot + 12),
            gap=gap,
        )
    elif kind == 1:
        command = NORMAL_COMMANDS[target]
        line.add(
            "Bash  %s" % command,
            tool=("Bash", {"command": command}, command),
            result="ok",
            gap=gap,
        )
    else:
        line.add(
            "Edit  %s" % path,
            tool=(
                "Edit",
                {
                    "file_path": path,
                    "old_string": "MARKER_%d" % slot,
                    "new_string": "MARKER_%d_RENAMED" % slot,
                },
                "",
            ),
            result="已写入",
            gap=gap,
        )


def _build_scenario() -> List[_Step]:
    """铺好 100 步场景。

    :return: 步骤列表。
    """
    line = _Timeline(BASE_TS)
    slot = 0

    # --- 第 1 - 24 步：正常干活 ---
    for _ in range(24):
        _normal_step(line, slot)
        slot += 1

    # --- 第 25 - 31 步：连续 Read 同一个文件 ---
    # 40 秒一次，6 次都在 200 秒内（远小于 10 分钟窗口）。
    for index in range(25, 32):
        expect = index == 30
        note = ""
        if index == 25:
            note = "第 1 次 —— 还没到阈值"
        elif index == 30:
            note = "第 6 次 —— 越过阈值，当场报警"
        elif index == 31:
            note = "第 7 次 —— 同一波不重复刷屏"
        line.add(
            "Read  %s" % STUCK_FILE,
            tool=("Read", {"file_path": STUCK_FILE}, ""),
            result="<文件正文>",
            gap=40.0,
            expect=expect,
            expect_verdict=VERDICT_STATS_LOOP if expect else "",
            note=note,
        )

    # --- 第 32 - 36 步：正常干活（窗口开始往下滑） ---
    for _ in range(5):
        _normal_step(line, slot)
        slot += 1

    # --- 第 37 - 42 步：又一轮 Read 同一个文件 ---
    # 旧记录已经被窗口挤掉，计数从低位重新爬 —— 第 6 次再次报警，
    # 说明「窗口滑过后重新武装」这条逻辑是活的。
    for index in range(37, 43):
        expect = index == 42
        note = ""
        if index == 37:
            note = "新一轮第 1 次 —— 窗口已滑走旧记录"
        elif index == 42:
            note = "新一轮第 6 次 —— 重新武装后再次报警"
        line.add(
            "Read  %s" % STUCK_FILE,
            tool=("Read", {"file_path": STUCK_FILE}, ""),
            result="<文件正文>",
            gap=40.0,
            expect=expect,
            expect_verdict=VERDICT_STATS_LOOP if expect else "",
            note=note,
        )

    # --- 第 43 - 46 步：无效编辑 ---
    line.add(
        "Edit  %s（改了等于没改）" % NORMAL_FILES[1],
        tool=(
            "Edit",
            {
                "file_path": NORMAL_FILES[1],
                "old_string": "def health():\n    return 'ok'\n",
                "new_string": "def health():\n    return 'ok'\n",
            },
            "",
        ),
        result="已写入",
        gap=30.0,
        expect=True,
        expect_verdict=VERDICT_STATS_NOOP,
        note="前后文本一模一样 —— 无效编辑",
    )
    line.add(
        "Edit  %s（只差空格 / 换行）" % NORMAL_FILES[2],
        tool=(
            "Edit",
            {
                "file_path": NORMAL_FILES[2],
                "old_string": "def audit():\n    return 1\n",
                "new_string": "def audit():\n    return 1",
            },
            "",
        ),
        result="已写入",
        gap=30.0,
        expect=True,
        expect_verdict=VERDICT_STATS_NOOP,
        note="只少了一个换行 —— 无效编辑",
    )
    line.add(
        "Edit  %s（有实质改动）" % NORMAL_FILES[3],
        tool=(
            "Edit",
            {
                "file_path": NORMAL_FILES[3],
                "old_string": "def box():\n    return 1\n",
                "new_string": "def box():\n    return 2\n",
            },
            "",
        ),
        result="已写入",
        gap=30.0,
        note="真的改了东西 —— 不该报",
    )
    line.add(
        "Edit  %s（拿不到前后证据）" % NORMAL_FILES[4],
        tool=("Edit", {"file_path": NORMAL_FILES[4]}, ""),
        result="已写入",
        gap=30.0,
        note="没有 old/new 文本 —— 宁可漏报，不猜",
    )

    # --- 第 47 - 48 步：极端摸鱼 ---
    line.add(
        "thinking（沉默 16 分钟之后）",
        gap=960.0,
        expect=True,
        expect_verdict=VERDICT_STATS_STALL,
        note="距上一次动作 16 分钟 —— 疑似卡死",
    )
    line.add(
        "thinking（缓过来了）",
        gap=30.0,
        note="恢复正常 —— 不该报",
    )

    # --- 第 49 - 100 步：继续正常干活 ---
    while len(line.steps) < 100:
        _normal_step(line, slot)
        slot += 1

    return line.steps


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------
def _run_detector(
    steps: List[_Step],
) -> Tuple[MicroStatsDetector, List[List[Any]]]:
    """把 100 步喂给**纯检测器**。

    :param steps: 步骤列表。
    :return: ``(检测器, 每一步的结论列表)``。
    """
    detector = MicroStatsDetector()
    per_step: List[List[Any]] = []
    for step in steps:
        findings: List[Any] = []
        for event in step.events:
            findings.extend(detector.observe(event))
        per_step.append(findings)
    return detector, per_step


class _CaptureHandler(logging.Handler):
    """把日志行抓到内存里（用来验收引擎写出的流水行）。

    :param lines: 抓到的行。
    """

    def __init__(self, lines: List[str]) -> None:
        super().__init__()
        self.lines = lines

    def emit(self, record: logging.LogRecord) -> None:
        """收下一条日志。

        :param record: 日志记录。
        :return: 无。
        """
        try:
            self.lines.append(record.getMessage())
        except Exception as exc:  # noqa: BLE001 —— 抓日志失败不中断演示
            logging.getLogger(__name__).warning(
                "抓日志失败：%s: %s", type(exc).__name__, exc
            )


class _DemoAdapter(BaseAdapter):
    """演示用适配器 —— 事件是现成的，它只负责占位。"""

    name = "demo"

    def parse_payload(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把一个载荷拆成事件（演示里用不上，返回空）。

        :param payload: 载荷。
        :return: 空列表。
        """
        return []


def _run_engine(steps: List[_Step]) -> List[str]:
    """用**真实引擎**跑一遍，验收「橘黄流水行」真的写出来了。

    :param steps: 步骤列表。
    :return: 引擎 logger 抓到的所有日志行。
    """
    lines: List[str] = []
    logger = logging.getLogger("demo.micro_stats.engine")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _CaptureHandler(lines)
    logger.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory(prefix="micro-stats-demo-") as tmp:
            engine = SentinelEngine(
                _DemoAdapter(),
                RuleEngine({}),
                console=False,
                alert_logger=logger,
                alert_log_path=os.path.join(tmp, "sentinel_alert.log"),
                workdir=tmp,
            )
            reporter = MicroStatsReporter()
            reporter.attach(engine)
            for step in steps:
                for event in step.events:
                    engine.process_event(event)
            # ⚠️ 必须关掉引擎 —— 它把告警日志的文件句柄攥在手里，
            #    Windows 上不关就删不掉临时目录（``PermissionError``）。
            engine.close()
    finally:
        logger.removeHandler(handler)
    return lines


def _parse_ontrack(line: str) -> Optional[Dict[str, Any]]:
    """用**看板自己**的解析器认领一条流水行。

    :param line: 日志行。
    :return: 流水条目；认领不到返回 ``None``。
    """
    try:
        from src.gui.server import parse_ontrack_line
    except ImportError as exc:
        _say("⚠️ 看板解析器不可用（%s: %s）—— 跳过流水区验收" % (
            type(exc).__name__,
            exc,
        ))
        return None
    return parse_ontrack_line(line)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _print_report(
    steps: List[_Step],
    per_step: List[List[Any]],
) -> Dict[str, Any]:
    """打印逐步拦截日志，并汇总结论。

    :param steps: 步骤列表。
    :param per_step: 每一步的结论。
    :return: 汇总字典。
    """
    _say("=" * 78)
    _say("微观统计学异常检测 · 100 步高频操作回放")
    _say("=" * 78)
    _say("")
    counts = {
        VERDICT_STATS_LOOP: 0,
        VERDICT_STATS_NOOP: 0,
        VERDICT_STATS_STALL: 0,
    }
    intercepted: List[int] = []
    false_positives: List[int] = []
    missed: List[int] = []
    for step, findings in zip(steps, per_step, strict=True):
        if findings:
            verdict = findings[0].verdict
            counts[verdict] = counts.get(verdict, 0) + 1
            intercepted.append(step.index)
            if not step.expect:
                false_positives.append(step.index)
            _say(
                "第 %3d 步 │ %s │ %s" % (
                    step.index,
                    _clock(step.events[0]["timestamp"]),
                    step.label,
                )
            )
            for finding in findings:
                _say(
                    "        └─ 🟠 已拦截（未进入大模型） %s"
                    % finding.reason
                )
            if step.note:
                _say("           · %s" % step.note)
        else:
            if step.expect:
                missed.append(step.index)
            _say(
                "第 %3d 步 │ %s │ %s%s"
                % (
                    step.index,
                    _clock(step.events[0]["timestamp"]),
                    step.label,
                    "   ← 漏报了！" if step.expect else "",
                )
            )
    _say("")
    _say("=" * 78)
    _say("验收结论")
    _say("=" * 78)
    total = len(steps)
    _say("总步数                  : %d" % total)
    _say("被拦截（未进大模型）    : %d" % len(intercepted))
    _say("照常送入大模型          : %d" % (total - len(intercepted)))
    _say("  ├ %s 重复动作      : %d" % (
        TAG_STATS_LOOP, counts[VERDICT_STATS_LOOP]))
    _say("  ├ %s 改了等于没改  : %d" % (
        TAG_INVALID_EDIT, counts[VERDICT_STATS_NOOP]))
    _say("  └ %s 长时间摸鱼    : %d" % (
        TAG_STALL, counts[VERDICT_STATS_STALL]))
    _say("误报（不该报却报了）    : %d%s" % (
        len(false_positives),
        "  ← 步号 %s" % false_positives if false_positives else "",
    ))
    _say("漏报（该报却没报）      : %d%s" % (
        len(missed),
        "  ← 步号 %s" % missed if missed else "",
    ))
    _say("")
    return {
        "total_steps": total,
        "intercepted_steps": len(intercepted),
        "passed_to_model": total - len(intercepted),
        "counts": dict(counts),
        "intercepted_indexes": intercepted,
        "false_positives": false_positives,
        "missed": missed,
    }


def _print_engine_evidence() -> Dict[str, Any]:
    """用真实引擎跑「同一条命令跑 4 次」，验收橘黄流水行。

    :return: 验收结果字典。
    """
    _say("")
    _say("=" * 78)
    _say("端到端：真实 SentinelEngine + MicroStatsReporter.attach()")
    _say("=" * 78)
    line = _Timeline(BASE_TS)
    for index in range(4):
        line.add(
            "Bash  %s" % STUCK_COMMAND,
            tool=("Bash", {"command": STUCK_COMMAND}, STUCK_COMMAND),
            result="2 passed in 0.31s",
            gap=40.0,
        )
    lines = _run_engine(line.steps)
    stats_lines = [
        text for text in lines if ONTRACK_MARKER in text and "micro_stats" in text
    ]
    if not stats_lines:
        _say("❌ 引擎没有写出微观统计流水行 —— 挂载失败")
        return {"engine_lines": 0, "parsed": None}
    raw = stats_lines[-1]
    _say("引擎写出的流水行（最后一条）：")
    _say("  " + raw[:200] + ("…" if len(raw) > 200 else ""))
    entry = _parse_ontrack(raw)
    if entry is None:
        _say("❌ 看板解析器认领不了这一行 —— 流水区收不到")
        return {"engine_lines": len(stats_lines), "parsed": None}
    _say("")
    _say("看板流水区认领结果：")
    _say("  audit_level  : %s   （warn = 橘黄）" % entry.get("audit_level"))
    _say("  audit_text   : %s" % entry.get("audit_text"))
    _say("  event_type   : %s" % entry.get("event_type"))
    _say("  verdict      : %s" % (entry.get("micro_stats") or {}).get("verdict"))
    _say("")
    return {
        "engine_lines": len(stats_lines),
        "parsed": {
            "audit_level": entry.get("audit_level"),
            "audit_text": entry.get("audit_text"),
            "event_type": entry.get("event_type"),
            "verdict": (entry.get("micro_stats") or {}).get("verdict"),
        },
    }


def main() -> int:
    """脚本入口。

    :return: 进程退出码（0 = 验收通过）。
    """
    parser = argparse.ArgumentParser(description="微观统计学异常检测验收演示")
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="把验收结论导出到这个 JSON 文件",
    )
    args = parser.parse_args()

    _force_utf8_stdout()

    steps = _build_scenario()
    _detector, per_step = _run_detector(steps)
    summary = _print_report(steps, per_step)
    summary["engine"] = _print_engine_evidence()

    ok = not summary["false_positives"] and not summary["missed"]
    summary["passed"] = ok
    _say("")
    if ok:
        _say("✅ 验收通过：误报 0、漏报 0，全部结论都在纯代码层产生。")
    else:
        _say("❌ 验收未通过：有误报或漏报。")

    if args.json_path:
        payload = {
            "summary": summary,
            "steps": [
                {
                    "index": step.index,
                    "label": step.label,
                    "note": step.note,
                    "expect": step.expect,
                    "findings": [f.to_dict() for f in findings],
                }
                for step, findings in zip(steps, per_step, strict=True)
            ],
        }
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _say("验收证据已导出：%s" % args.json_path)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
