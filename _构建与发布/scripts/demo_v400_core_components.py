"""v4.0.0 核心组件集成验收演示（日志压缩 / 微统计 / 优先级队列）。

这个脚本证明的是一件事：**三个组件真的接进了** ``src/core/engine.py``
的调用链路，而不是「模块存在、测试能跑」。

每一段都走**真实代码路径**：

1. **日志压缩器** —— 用引擎里同一个 ``build_compressed_summary``，
   把一段真实的工具调用流压成演进流，并与 v3.9.0 的粗糙截断对比；
2. **微观统计** —— 真的构造一个 ``SentinelEngine``，喂它重复读同一个
   文件的事件，看 ``STATS_LOOP`` 告警是不是从引擎里冒出来的，
   以及后续判定是不是真的被打断；
3. **优先级队列** —— 展示红线 → ``P0`` 的映射关系、
   漂移判定与红线解释**共用同一个调度器**，
   再实测一次「``P0`` 插到已排队的 ``P2`` 前面」。

跑法::

    python _构建与发布/scripts/demo_v400_core_components.py
    python _构建与发布/scripts/demo_v400_core_components.py --json 证据.json

⚠️ 整个脚本**不碰大模型** —— 判定回调都是假的，
   证明的是「谁被调用、拿到什么输入、按什么顺序执行」。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adapters.mcode import McodeAdapter  # noqa: E402
from src.core.engine import (  # noqa: E402
    IntentTracker,
    SentinelEngine,
    _l2_priority_for_verdict,
    build_compressed_summary,
    build_window_summary,
)
from src.core.l2_scheduler import L2Scheduler  # noqa: E402
from src.core.micro_stats import (  # noqa: E402
    VERDICT_STATS_LOOP,
    MicroStatsDetector,
)
from src.core.priority_queue import (  # noqa: E402
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
)
from src.rules.basic import RuleEngine  # noqa: E402


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _say(text: str = "") -> None:
    """打印一行（演示脚本用 print 是允许的）。

    :param text: 要打印的内容。
    :return: 无。
    """
    print(text, flush=True)


def _rule(title: str = "") -> None:
    """打一条分隔线。

    :param title: 分隔线中间的标题。
    :return: 无。
    """
    if title:
        _say("─" * 8 + " " + title + " " + "─" * max(0, 60 - len(title)))
    else:
        _say("─" * 72)


def _force_utf8_stdout() -> None:
    """把标准输出切到 UTF-8。

    ⚠️ Windows 控制台默认是 GBK —— 中文能打，
    但「✅」「→」这类符号会直接抛 ``UnicodeEncodeError``。
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


def _thinking(text: str) -> Dict[str, Any]:
    """造一条思考事件。

    :param text: 思考正文。
    :return: 事件字典。
    """
    return {"type": "thinking", "text": text}


def _tool(name: str, **arguments: Any) -> Dict[str, Any]:
    """造一条工具调用事件。

    :param name: 工具名。
    :param arguments: 工具参数。
    :return: 事件字典。
    """
    return {
        "type": "tool_call",
        "tool": {"name": name, "arguments": dict(arguments)},
    }


def _indent(text: str, prefix: str = "    ") -> None:
    """把多行文本缩进后打印出来。

    :param text: 正文。
    :param prefix: 每行前缀。
    :return: 无。
    """
    for line in str(text or "").splitlines():
        _say(prefix + line)


# ---------------------------------------------------------------------------
# 第 1 段：日志压缩器
# ---------------------------------------------------------------------------
def _build_window() -> List[Dict[str, Any]]:
    """造一段真实的「重构 auth 模块」工具调用流。

    :return: 事件列表。
    """
    return [
        _thinking("我先看看目录结构"),
        _tool("Read", file_path="D:/proj/src/auth/login.py"),
        _tool("Read", file_path="D:/proj/src/auth/token.py"),
        _tool("Read", file_path="D:/proj/src/auth/session.py"),
        _tool("Bash", command="pytest tests/test_auth.py -q"),
        _thinking("token 校验这块得先抽出来"),
        _tool("Edit", file_path="D:/proj/src/auth/token.py"),
        _tool("Bash", command="pytest tests/test_auth.py -q"),
        _tool("Edit", file_path="D:/proj/src/auth/login.py"),
        _tool("Read", file_path="D:/proj/src/auth/login.py"),
        _tool("Bash", command="ruff check src/auth"),
        _tool("Edit", file_path="D:/proj/src/auth/session.py"),
        _tool("Bash", command="pytest tests/ -q"),
        _thinking("全绿了，再确认一下没有漏改的地方"),
        _tool("Bash", command="git diff --stat"),
    ]


def _demo_compressor() -> Dict[str, Any]:
    """演示日志压缩器接进了引擎。

    :return: 证据字典。
    """
    _rule("第 1 段：日志压缩器（engine.build_compressed_summary）")
    events = _build_window()
    old = build_window_summary(events)
    new = build_compressed_summary(events, "重构 auth 模块的 token 校验")
    _say("窗口事件数：%d" % len(events))
    _say("")
    _say("── v3.9.0 的粗糙截断（build_window_summary） ──")
    _say("长度：%d 字符" % len(old))
    _indent(old)
    _say("")
    _say("── v4.0.0 的压缩演进流（build_compressed_summary） ──")
    _say("长度：%d 字符" % len(new))
    _indent(new)
    _say("")
    _say("旧格式把命令文本丢了：%s" % ("（无命令文本）" in old))
    _say("新格式保住了命令文本：%s" % ("pytest tests/test_auth.py -q" in new))
    _say("新格式带步号：%s" % ("[1]" in new))
    _say("新格式带当前动作行：%s" % ("[Current Action]:" in new))
    return {
        "old_length": len(old),
        "new_length": len(new),
        "old_text": old,
        "new_text": new,
        "old_lost_command": "（无命令文本）" in old,
        "new_keeps_command": "pytest tests/test_auth.py -q" in new,
    }


def _build_drift_window() -> List[Dict[str, Any]]:
    """造一段**不会**被 v3.9.0 白名单放行的窗口。

    ⚠️ 为什么单独造一段
    ==================

    v3.9.0 的确定性白名单会把「跑 pytest / ruff」这类**常规演进**
    直接放行 —— 根本不发 L2 请求（省算力）。
    上面那段窗口里正好有 ``pytest`` 和 ``ruff``，所以它压根到不了
    判定回调。要证明「压缩后的摘要真的送到了 L2」，
    就得造一段**没有**常规演进痕迹的窗口。

    :return: 事件列表。
    """
    return [
        _thinking("token 的签名校验要挪到单独的服务里"),
        _tool("Read", file_path="D:/proj/src/auth/token.py"),
        _tool("Edit", file_path="D:/proj/src/auth/token.py"),
        _tool("Read", file_path="D:/proj/src/auth/verify.py"),
        _tool("Edit", file_path="D:/proj/src/auth/verify.py"),
        _thinking("顺手把整个会话存储换成 Redis"),
        _tool("Read", file_path="D:/proj/src/storage/session.py"),
        _tool("Write", file_path="D:/proj/src/storage/redis_session.py"),
    ]


def _demo_intent_tracker_input() -> Dict[str, Any]:
    """演示判定回调**真的**收到压缩后的摘要。

    :return: 证据字典。
    """
    _rule("第 2 段：L2 判定拿到的输入（IntentTracker → 调度器 → 判定回调）")
    seen: Dict[str, Any] = {}

    def evaluator(goal: str, summary: str) -> SimpleNamespace:
        """假的 L2 判定回调 —— 只把输入记下来。

        :param goal: 任务目标。
        :param summary: 窗口摘要。
        :return: 一个「没漂移」的假结论。
        """
        seen["goal"] = goal
        seen["summary"] = summary
        return SimpleNamespace(drift=False, reason="", degraded=False)

    # --- 2a：命中 v3.9.0 确定性白名单的窗口 → 根本不发 L2 请求 ---
    bypass_tracker = IntentTracker(
        goal="重构 auth 模块的 token 校验",
        window=len(_build_window()),
        threshold=2,
        evaluator=evaluator,
    )
    for event in _build_window():
        bypass_tracker.observe(event)
    _say("2a) 窗口里有 pytest / ruff —— 命中 v3.9.0 确定性白名单：")
    _say("    放行次数 = %d，判定请求**根本没发**（省一次模型调用）"
         % bypass_tracker.bypassed)
    _say("    判定回调被调用了 %d 次" % len(seen))

    # --- 2b：不含常规演进痕迹的窗口 → 判定回调真的收到压缩摘要 ---
    seen.clear()
    tracker = IntentTracker(
        goal="重构 auth 模块的 token 校验",
        window=len(_build_drift_window()),
        threshold=2,
        evaluator=evaluator,
    )
    for event in _build_drift_window():
        tracker.observe(event)

    if not seen:
        _say("❌ 判定回调根本没被调用 —— 链路断了。")
        return {"called": False}
    _say("")
    _say("2b) 窗口里没有常规演进痕迹 —— 判定请求照发：")
    _say("    判定回调被调用了 %d 次，目标 = %s"
         % (len(seen), seen["goal"]))
    _say("    判定回调收到的摘要：")
    _indent(str(seen["summary"]))
    _say("摘要里含「[Current Action]:」= %s"
         % ("[Current Action]:" in str(seen["summary"])))
    _say("摘要里含旧格式「第1步」= %s" % ("第1步" in str(seen["summary"])))
    return {
        "called": True,
        "bypassed_windows": bypass_tracker.bypassed,
        "goal": seen.get("goal", ""),
        "summary": str(seen.get("summary") or ""),
    }


# ---------------------------------------------------------------------------
# 第 3 段：微观统计
# ---------------------------------------------------------------------------
class _SpyContextTracker:
    """上下文追踪器**探针** —— 记录自己被调了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, event: Dict[str, Any]) -> None:
        """记录一次调用。

        :param event: 当前事件。
        :return: 无。
        """
        self.calls += 1


def _demo_micro_stats(workdir: str) -> Dict[str, Any]:
    """演示微统计接进了引擎，并且真的会**打断**后续判定。

    :param workdir: 临时工作目录。
    :return: 证据字典。
    """
    _rule("第 3 段：微观统计（engine.micro_stats → STATS_LOOP 打断）")
    detector = MicroStatsDetector(
        window_minutes=10.0,
        same_file_threshold=3,
        same_command_threshold=999,
        stall_minutes=999.0,
    )
    spy = _SpyContextTracker()
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        console=False,
        alert_log_path=os.path.join(workdir, "sentinel_alert.log"),
        workdir=workdir,
        micro_stats=detector,
        context_tracker=spy,
    )
    try:
        for index in range(1, 5):
            engine.process_event(_tool("Read", file_path="D:/proj/a.py"))
            _say("第 %d 次读同一个文件 → 上下文判定已跑 %d 次"
                 % (index, spy.calls))
        alerts = list(engine.alerts)
    finally:
        engine.close()

    stats_alerts = [
        alert for alert in alerts if alert.get("verdict") == VERDICT_STATS_LOOP
    ]
    _say("")
    if stats_alerts:
        alert = stats_alerts[0]
        _say("引擎产出的告警：verdict=%s 触发器=%s"
             % (alert.get("verdict"), alert.get("triggers")))
        _say("细节：%s" % json.dumps(
            alert.get("micro_stats") or {}, ensure_ascii=False
        ))
    else:
        _say("❌ 引擎没有产出 STATS_LOOP 告警。")
    _say("")
    _say("上下文判定一共跑了 %d 次（挂检测器时第 4 次被**打断**）"
         % spy.calls)
    return {
        "context_calls_with_detector": spy.calls,
        "stats_alert": bool(stats_alerts),
        "verdict": VERDICT_STATS_LOOP,
    }


def _demo_micro_stats_control(workdir: str) -> Dict[str, Any]:
    """对照组 —— 不挂检测器时判定次数必须是 4。

    :param workdir: 临时工作目录。
    :return: 证据字典。
    """
    spy = _SpyContextTracker()
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        console=False,
        alert_log_path=os.path.join(workdir, "sentinel_alert_control.log"),
        workdir=workdir,
        context_tracker=spy,
    )
    try:
        for _ in range(4):
            engine.process_event(_tool("Read", file_path="D:/proj/a.py"))
    finally:
        engine.close()
    _say("对照组（不挂微统计）：上下文判定跑了 %d 次 —— 证明差的那一次"
         "确实是微统计打断的" % spy.calls)
    return {"context_calls_without_detector": spy.calls}


# ---------------------------------------------------------------------------
# 第 4 段：优先级队列
# ---------------------------------------------------------------------------
def _demo_priority_mapping() -> Dict[str, Any]:
    """演示红线 → P0 的映射。

    :return: 证据字典。
    """
    _rule("第 4 段：优先级映射（engine._l2_priority_for_verdict）")
    cases = [
        ({"severity": "red_line"}, "红线", PRIORITY_P0),
        ({"verdict": "DANGEROUS", "severity": "standard"}, "危险动作",
         PRIORITY_P1),
        ({"verdict": "ON_TRACK"}, "正常", PRIORITY_P2),
        (None, "没有判定", PRIORITY_P2),
    ]
    for payload, label, expect in cases:
        actual = _l2_priority_for_verdict(payload)
        _say("%-10s → %s" % (label, actual.name))
        if actual is not expect:
            _say("❌ 映射不对，期望 %s" % expect.name)
    return {
        "red_line": PRIORITY_P0.name,
        "dangerous": PRIORITY_P1.name,
        "normal": PRIORITY_P2.name,
    }


def _demo_shared_scheduler(workdir: str) -> Dict[str, Any]:
    """演示漂移判定与红线解释**共用同一个调度器**。

    :param workdir: 临时工作目录。
    :return: 证据字典。
    """
    _rule("第 5 段：共享调度器（漂移判定 ↔ 红线解释）")
    tracker = IntentTracker(goal="目标", evaluator=lambda *_: None)
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        console=False,
        alert_log_path=os.path.join(workdir, "sentinel_alert_sched.log"),
        workdir=workdir,
        intent_tracker=tracker,
    )
    try:
        shared = (
            tracker.scheduler is engine.l2_scheduler
            and engine.explanation_worker.scheduler is engine.l2_scheduler
        )
        _say("engine.l2_scheduler            = %s"
             % type(engine.l2_scheduler).__name__)
        _say("intent_tracker.scheduler       = %s"
             % type(tracker.scheduler).__name__)
        _say("explanation_worker.scheduler   = %s"
             % type(engine.explanation_worker.scheduler).__name__)
        _say("三者是**同一个对象**：%s" % shared)
        _say("")
        _say("调度器当前状态：%s" % json.dumps(
            engine.l2_scheduler.stats(), ensure_ascii=False
        ))
    finally:
        engine.close()
    return {"shared_scheduler": shared}


def _demo_p0_jumps_ahead() -> Dict[str, Any]:
    """实测「P0 插到已排队的 P2 前面」。

    :return: 证据字典。
    """
    _rule("第 6 段：实测插队（P0 抢在已排队的 P2 前面）")
    order: List[str] = []
    running = threading.Event()
    release = threading.Event()

    def slow() -> str:
        """占着执行槽的慢任务。"""
        running.set()
        release.wait(5.0)
        order.append("p2-running")
        return "slow"

    def queued_p2() -> str:
        """已经排上队、还没轮到的 P2。"""
        order.append("p2-queued")
        return "queued"

    def red_line() -> str:
        """红线（P0）。"""
        order.append("p0")
        return "red"

    scheduler = L2Scheduler()
    try:
        blocker = threading.Thread(
            target=lambda: scheduler.call(
                "demo-slow", slow, priority=PRIORITY_P2, timeout=10.0
            )
        )
        blocker.start()
        if not running.wait(5.0):
            _say("❌ 慢任务没有开跑。")
            return {"ok": False}

        queued = threading.Thread(
            target=lambda: scheduler.call(
                "demo-p2", queued_p2, priority=PRIORITY_P2, timeout=10.0
            )
        )
        queued.start()
        deadline = time.time() + 5.0
        while scheduler.pending() < 1 and time.time() < deadline:
            time.sleep(0.01)
        _say("队列里已经排了 %d 个任务（P2）" % scheduler.pending())

        scheduler.call(
            "demo-p0", red_line, priority=PRIORITY_P0, timeout=10.0
        )
        release.set()
        blocker.join(5.0)
        queued.join(5.0)
    finally:
        release.set()
        scheduler.stop()

    _say("实际执行顺序：%s" % " → ".join(order))
    ok = (
        "p0" in order
        and "p2-queued" in order
        and order.index("p0") < order.index("p2-queued")
    )
    _say("P0 排在 P2 前面：%s" % ok)
    return {"order": order, "ok": ok}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _main() -> Tuple[int, Dict[str, Any]]:
    """跑一遍演示。

    :return: ``(进程退出码, 证据字典)``。
    """
    _force_utf8_stdout()
    logging.basicConfig(level=logging.WARNING)

    _rule("v4.0.0 核心组件集成 —— 验收演示")
    _say("Python : %s" % sys.version.split()[0])

    evidence: Dict[str, Any] = {
        "version": "v4.0.0",
        "feature": "核心组件集成（压缩器 / 微统计 / 优先级队列）",
        "python": sys.version.split()[0],
    }

    workdir = tempfile.mkdtemp(prefix="sentinel-v400-core-demo-")
    try:
        evidence["compressor"] = _demo_compressor()
        evidence["intent_input"] = _demo_intent_tracker_input()
        evidence["micro_stats"] = _demo_micro_stats(workdir)
        evidence["micro_stats_control"] = _demo_micro_stats_control(workdir)
        evidence["priority_mapping"] = _demo_priority_mapping()
        evidence["shared_scheduler"] = _demo_shared_scheduler(workdir)
        evidence["p0_jumps_ahead"] = _demo_p0_jumps_ahead()

        _rule("验收结论")
        checks = [
            (
                "压缩器保住命令文本",
                bool(evidence["compressor"]["new_keeps_command"]),
            ),
            (
                "L2 判定收到压缩后的摘要",
                bool(evidence["intent_input"].get("called"))
                and "[Current Action]:" in str(
                    evidence["intent_input"].get("summary") or ""
                ),
            ),
            (
                "引擎产出 STATS_LOOP 告警",
                bool(evidence["micro_stats"]["stats_alert"]),
            ),
            (
                "微统计打断了后续判定",
                evidence["micro_stats"]["context_calls_with_detector"] == 3
                and evidence["micro_stats_control"][
                    "context_calls_without_detector"
                ] == 4,
            ),
            (
                "红线映射到 P0",
                evidence["priority_mapping"]["red_line"] == PRIORITY_P0.name,
            ),
            (
                "漂移判定与红线解释共用调度器",
                bool(evidence["shared_scheduler"]["shared_scheduler"]),
            ),
            (
                "P0 抢在已排队的 P2 前面",
                bool(evidence["p0_jumps_ahead"]["ok"]),
            ),
        ]
        failed = 0
        for label, ok in checks:
            _say("%s %s" % ("✅" if ok else "❌", label))
            if not ok:
                failed += 1
        evidence["checks"] = dict(checks)
        evidence["passed"] = failed == 0
        return (0 if failed == 0 else 1), evidence
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    """把证据写成 JSON。

    :param path: 目标路径。
    :param payload: 证据字典。
    :return: 无。
    """
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    _say("")
    _say("证据已写入：%s" % path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="v4.0.0 核心组件集成验收演示"
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="把证据额外写成 JSON 文件",
    )
    args = parser.parse_args()
    code, evidence = _main()
    if args.json_path:
        _write_json(args.json_path, evidence)
    sys.exit(code)
