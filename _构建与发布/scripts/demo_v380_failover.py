"""v3.8.0 演示：L2 掉线**热切 L3** 与 **L3 回退预算熔断**（Mock）。

这一版给级联补了一条断层：本地模型（NInfer）突然掉线时，
不再只能干瞪眼回退 L1，而是把**原本给 L2 的载荷**转给云端 L3。

本脚本用 **Mock 提供方**把三种结局真跑一遍并打印出来：

1. **热切成功** —— L2 连不上 → 载荷转给 L3 → 流水提示
   ``[WARN] L2 掉线，已自动切换至 L3 深度审查``；
2. **预算熔断** —— 窗口内热切次数超阈值 → 放弃云端调用、
   回退 L1 纯规则，并发一条严重系统告警；
3. **L3 也挂** —— 安静回退 L1：不报错、不卡死。

⚠️ **热切是单盲** —— 只有 L3 一票，所以**绝不亮红灯**
（「双盲一致才升级红线」这条原则一字未改）。

用法::

    python _构建与发布/scripts/demo_v380_failover.py
    python _构建与发布/scripts/demo_v380_failover.py --screenshot
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _extra in (_ROOT, _HERE):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import console_bridge  # noqa: E402 —— 必须先补 sys.path

from src.llm.cascade import (  # noqa: E402 —— 必须先补 sys.path
    CASCADE_BUDGET_NOTICE,
    CASCADE_FAILOVER_NOTICE,
    CASCADE_STATUS_FAILOVER,
    CASCADE_STATUS_UNAVAILABLE,
    CascadeRouter,
)

#: 证据 JSON 的落点。
ASSETS = os.path.join(_ROOT, "_管理文档", "文档与报告", "assets")

#: 演示控制台窗口的标题（**纯 ASCII** —— 便于按标题定位窗口）。
CONSOLE_TITLE = "SENTINEL-L2-FAILOVER-L3-MOCK"

#: 控制台窗口在屏幕上的位置与大小（逻辑像素）。
#:
#: ⚠️ 窗口要开得**足够宽、足够高** —— 实测在 ConPTY 下：
#:   · 窄了文字会被窗口右缘裁掉；
#:   · Windows Terminal 的行高约 33 像素，1040 像素高
#:     只装得下约 29 行 —— 所以输出必须**分段**，否则顶部被滚掉。
#:
#: 所以这里按**整屏宽度**开窗口，并按段拆分输出。
CONSOLE_X = 8
CONSOLE_Y = 16
CONSOLE_W = 1690
CONSOLE_H = 1040

#: 每块输出属于哪一段。
#:
#: ⚠️ 为什么要分段：控制台窗口装不下全部 60 行，
#: 硬塞进去文字会被下缘滚掉。所以分三段各拍一张，
#: 三张合起来才是完整证据。
SEGMENT_OF = {
    "failover": "a",
    "budget": "b",
    "both_down": "c",
    "assert": "c",
}

#: 三段截图的分段标题。
SEGMENT_TITLES = {
    "a": "第 1/3 段：L2 掉线 → 载荷热切 L3",
    "b": "第 2/3 段：L3 回退预算熔断",
    "c": "第 3/3 段：L3 也挂时安静回退 L1 + 断言核对",
}


class _MockL2:
    """Mock 的 L2 本地模型 —— 永远「掉线」。"""

    def __init__(self, reason: str = "ConnectionError（连接被拒绝）") -> None:
        self.model = "mock-ninfer-l2"
        self.reason = reason
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        """模拟本地引擎连不上。

        :param system: 系统提示。
        :param user:   用户提示。
        :raises ConnectionError: 永远抛 —— 这就是「L2 掉线」。
        """
        self.calls += 1
        raise ConnectionError(self.reason)


class _MockL3:
    """Mock 的 L3 云端模型 —— 按剧本回答。

    :param verdict: 三值判定（``ON_TRACK`` / ``DRIFT`` / ``DANGEROUS``）。
    :param down:    是否模拟「L3 也挂了」。
    """

    def __init__(self, verdict: str = "ON_TRACK", down: bool = False) -> None:
        self.model = "mock-cloud-l3"
        self.verdict = verdict
        self.down = down
        self.calls: List[str] = []

    def chat(self, system: str, user: str) -> str:
        """返回一次判定。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: JSON 判定文本。
        :raises ConnectionError: ``down=True`` 时模拟云端也挂。
        """
        self.calls.append(user)
        if self.down:
            raise ConnectionError("ConnectionError（云端也连不上）")
        return json.dumps(
            {"verdict": self.verdict, "reason": "Mock 云端复核"},
            ensure_ascii=False,
        )


def _rule(char: str = "-", width: int = 74) -> str:
    """画一条分隔线。

    :param char:  字符。
    :param width: 宽度。
    :return: 分隔线。
    """
    return char * width


def _show(title: str) -> List[str]:
    """生成一个小标题的打印行。

    :param title: 标题。
    :return: 打印行列表。
    """
    return ["", _rule("="), "  " + title, _rule("=")]


class _LogSink(logging.Handler):
    """收集库的日志，让它能**内联**到演示自己的打印行里。

    ⚠️ 为什么要内联而不是直接打到终端：
    分段截图时，别的段的日志会串到本段的画面里，
    看着像本段出的问题。收集之后按段内联，
    每张截图里只有它自己那段产生的日志。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志收进行内。

        :param record: 日志记录。
        :return: 无。
        """
        try:
            self.records.append("  [日志] {}".format(record.getMessage()))
        except Exception:  # noqa: BLE001,S110 —— 收集失败不该拖垮演示
            pass

    def drain(self) -> List[str]:
        """取走并清空已收集的日志行。

        :return: 日志行列表。
        """
        items = list(self.records)
        self.records.clear()
        return items


def _demo_failover(sink: _LogSink) -> Tuple[dict, List[str]]:
    """演示一：L2 掉线 → 热切 L3。

    :param sink: 日志收集器。
    :return: ``(证据字典, 打印行列表)``。
    """
    lines = _show("演示一：L2 掉线 → 载荷热切 L3")
    l2 = _MockL2()
    l3 = _MockL3(verdict="DRIFT")
    router = CascadeRouter(l2, l3)

    lines.append("  L2 状态      ：连不上（ConnectionError）")
    lines.append("  L3 状态      ：在线（Mock 云端）")
    lines.append("  热切开关     ：开（failover_to_l3=True）")
    lines.append("  " + _rule())
    result = router.filter(
        summary="最近读了 core/auth.py 与 core/user.py",
        action="Read core/session.py",
        l1_verdict="",
        l1_reason="",
    )
    lines.extend(sink.drain())
    lines.append("  级联结果     ：status={}".format(result.status))
    lines.append("  热切标记     ：failover={}".format(result.failover))
    lines.append("  单盲标记     ：bridged={}".format(result.bridged))
    lines.append("  是否亮红灯   ：red_light={}".format(result.red_light))
    lines.append("  流水提示     ：{}".format(result.notice))
    lines.append("  人话理由     ：{}".format(result.reason))
    lines.append("  L3 调用次数 ：{}".format(len(l3.calls)))
    lines.append("  L3 收到的载荷里有原动作吗：{}".format(
        "Read core/session.py" in (l3.calls[0] if l3.calls else "")
    ))
    lines.append("  L3 收到的载荷里有 L2 掉线原因吗：{}".format(
        "L2 掉线" in (l3.calls[0] if l3.calls else "")
    ))
    evidence = {
        "status": result.status,
        "failover": result.failover,
        "bridged": result.bridged,
        "red_light": result.red_light,
        "notice": result.notice,
        "reason": result.reason,
        "l3_calls": len(l3.calls),
        "payload_kept": "Read core/session.py" in (l3.calls[0] if l3.calls else ""),
        "l2_reason_forwarded": "L2 掉线" in (l3.calls[0] if l3.calls else ""),
    }
    return evidence, lines


def _demo_budget(sink: _LogSink) -> Tuple[dict, List[str]]:
    """演示二：L3 回退预算熔断。

    :param sink: 日志收集器。
    :return: ``(证据字典, 打印行列表)``。
    """
    lines = _show("演示二：L3 回退预算熔断（窗口内最多 2 次热切）")
    l2 = _MockL2()
    l3 = _MockL3(verdict="DRIFT")
    router = CascadeRouter(
        l2,
        l3,
        # ⚠️ 熔断阈值调高 —— 这一段要证明的停止理由**只有一个**：
        #    「预算用完了」，不是「L2 熔断了」。
        breaker_threshold=100,
        l3_fallback_limit=2,
        l3_fallback_limit_seconds=3600.0,
    )

    lines.append("  预算上限     ：3600 秒内最多 2 次热切")
    lines.append("  " + _rule())
    rows = []
    for index in range(1, 4):
        result = router.filter(
            summary="s",
            action="Read core/session.py",
            l1_verdict="",
            l1_reason="",
        )
        row = {
            "第几次": index,
            "status": result.status,
            "budget_exhausted": result.budget_exhausted,
            "notice": result.notice,
        }
        rows.append(row)
        lines.extend(sink.drain())
        lines.append("  第 {} 次 → status={} | 熔断={} | {}".format(
            index,
            result.status,
            result.budget_exhausted,
            result.notice or "（正常热切）",
        ))
    stats = router.stats()
    lines.append("  " + _rule())
    lines.append("  L3 实际调用 ：{} 次（熔断后不再调用）".format(stats["l3_calls"]))
    lines.append("  热切次数     ：{}".format(stats["failover"]))
    lines.append("  熔断次数     ：{}".format(stats["budget_exhausted"]))
    lines.append("  预算用量     ：{}/{} 次".format(
        stats["l3_fallback_budget"]["used"],
        stats["l3_fallback_budget"]["limit"],
    ))
    lines.append("  人话理由     ：{}".format(rows[-1]["notice"]))
    evidence = {
        "rows": rows,
        "l3_calls": stats["l3_calls"],
        "failover": stats["failover"],
        "budget_exhausted": stats["budget_exhausted"],
        "budget": stats["l3_fallback_budget"],
    }
    return evidence, lines


def _demo_both_down(sink: _LogSink) -> Tuple[dict, List[str]]:
    """演示三：L3 也挂了 → 安静回退 L1。

    :param sink: 日志收集器。
    :return: ``(证据字典, 打印行列表)``。
    """
    lines = _show("演示三：L3 也挂了 → 安静回退 L1（不报错、不卡死）")
    l2 = _MockL2()
    l3 = _MockL3(down=True)
    router = CascadeRouter(l2, l3)

    result = router.filter(
        summary="s",
        action="Read core/session.py",
        l1_verdict="",
        l1_reason="",
    )
    lines.extend(sink.drain())
    lines.append("  级联结果     ：status={}".format(result.status))
    lines.append("  是否亮红灯   ：red_light={}".format(result.red_light))
    lines.append("  是否熔断     ：budget_exhausted={}".format(
        result.budget_exhausted
    ))
    lines.append("  流水提示     ：{}".format(result.notice))
    lines.append("  人话理由     ：{}".format(result.reason))
    lines.append("  回退到哪     ：L1 纯规则（引擎继续按 L1 判定走）")
    evidence = {
        "status": result.status,
        "red_light": result.red_light,
        "budget_exhausted": result.budget_exhausted,
        "notice": result.notice,
        "reason": result.reason,
    }
    return evidence, lines


def _assertions(evidence: dict) -> List[str]:
    """核对演示结论 —— 任何一条不成立就抛错。

    :param evidence: 证据字典。
    :return: 断言说明列表。
    """
    checks = []

    failover = evidence["failover"]
    assert failover["status"] == CASCADE_STATUS_FAILOVER, "热切状态不对"
    assert failover["failover"] is True, "热切标记不对"
    assert failover["notice"] == CASCADE_FAILOVER_NOTICE, "提示语不对"
    assert failover["red_light"] is False, "热切**绝不能**亮红灯"
    assert failover["l3_calls"] == 1, "L3 应该被调用一次"
    assert failover["payload_kept"] is True, "原载荷没转给 L3"
    assert failover["l2_reason_forwarded"] is True, "L2 掉线原因没转给 L3"
    checks.append("热切：载荷转给 L3、提示语正确、绝不亮红灯")

    budget = evidence["budget"]
    assert budget["rows"][0]["budget_exhausted"] is False, "第 1 次不该熔断"
    assert budget["rows"][1]["budget_exhausted"] is False, "第 2 次不该熔断"
    assert budget["rows"][2]["budget_exhausted"] is True, "第 3 次该熔断"
    assert budget["rows"][2]["notice"] == CASCADE_BUDGET_NOTICE, "熔断提示语不对"
    assert budget["l3_calls"] == 2, "熔断后不该再调 L3"
    assert budget["budget_exhausted"] == 1, "熔断计数不对"
    checks.append("预算：第 3 次熔断、放弃云端调用、回退 L1")

    both = evidence["both_down"]
    assert both["status"] == CASCADE_STATUS_UNAVAILABLE, "回退状态不对"
    assert both["red_light"] is False, "回退**绝不能**亮红灯"
    assert both["budget_exhausted"] is False, "这不是熔断场景"
    checks.append("L3 也挂：安静回退 L1、不报错、不卡死")

    return checks


def _run_demo(segment: Optional[str] = None) -> dict:
    """跑三段演示（可只打印其中一段）。

    ⚠️ 三段**全都会真跑**（断言也在全跑），
    只是打印时按分段裁剪 —— 这样每一段截图里的结论都是
    基于完整证据算出来的，不是分开跑的。

    :param segment: ``None`` = 全打印；``"a"`` / ``"b"`` / ``"c"`` = 只打印该段。
    :return: 证据字典。
    """
    print("")
    print(_rule("="))
    print("  哨兵 v3.8.0 演示 —— L2 掉线热切 L3 / L3 回退预算熔断")
    title = SEGMENT_TITLES.get(segment or "")
    if title:
        print("  " + title)
    print("  （全部用 Mock 提供方，不连任何真实模型）")
    print(_rule("="))

    # 把库的日志接管过来 —— 这样它能内联到**各自那一段**，
    # 别的段的日志不会串到本段的画面里。
    sink = _LogSink()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.addHandler(sink)
    root.setLevel(logging.WARNING)

    evidence: dict = {}
    try:
        failover, lines = _demo_failover(sink)
        evidence["failover"] = failover
        if segment is None or SEGMENT_OF["failover"] == segment:
            print("\n".join(lines))

        budget, lines = _demo_budget(sink)
        evidence["budget"] = budget
        if segment is None or SEGMENT_OF["budget"] == segment:
            print("\n".join(lines))

        both_down, lines = _demo_both_down(sink)
        evidence["both_down"] = both_down
        if segment is None or SEGMENT_OF["both_down"] == segment:
            print("\n".join(lines))

        checks = _assertions(evidence)
        if segment is None or SEGMENT_OF["assert"] == segment:
            for line in _show("断言核对"):
                print(line)
            for index, text in enumerate(checks, start=1):
                print("  [{}] {}".format(index, text))
            print("  → {} 项断言全部通过".format(len(checks)))
    finally:
        root.removeHandler(sink)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
    return evidence


def _write_evidence(evidence: dict) -> str:
    """把证据写进 JSON 文件。

    :param evidence: 证据字典。
    :return: 文件路径。
    """
    os.makedirs(ASSETS, exist_ok=True)
    path = os.path.join(ASSETS, "v380-failover-evidence.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
    return path


def _console_title(segment: Optional[str]) -> str:
    """给这一段演示起一个窗口标题。

    :param segment: 分段标记。
    :return: 窗口标题（纯 ASCII）。
    """
    if segment:
        return "{}-{}".format(CONSOLE_TITLE, segment.upper())
    return CONSOLE_TITLE


def _console_run() -> int:
    """``--console-run`` 模式：在**自己所在的新控制台**里跑演示。

    由父进程用 ``CREATE_NEW_CONSOLE`` 拉起本模式；
    本进程已经附着在那块新控制台上，
    所以这里把输出接上去再跑。

    :return: 退出码。
    """
    segment = os.environ.get("SENTINEL_DEMO_SEGMENT", "") or None
    exit_file = os.environ.get("SENTINEL_DEMO_EXITFILE", "")
    hold = os.environ.get("SENTINEL_DEMO_HOLD", "") == "1"

    console_bridge.attach_console()
    console_bridge.prepare_console(_console_title(segment))
    console_bridge.make_utf8_stdout()

    code = 0
    evidence: dict = {}
    try:
        evidence = _run_demo(segment)
    except AssertionError as exc:
        print("  [错误] 断言不成立：{}".format(exc))
        code = 1
    path = _write_evidence(evidence)
    print("")
    print("  证据 JSON：{}".format(path))
    if exit_file:
        try:
            with open(exit_file, "w", encoding="utf-8") as handle:
                handle.write("EXITCODE={}\n".format(code))
        except OSError:
            pass
    if hold:
        print("")
        print("[demo] window held open for screenshot; press Enter to close.")
        try:
            sys.stdin.readline()
        except (OSError, ValueError):
            time.sleep(30)
    return code


def _capture_segment(segment: str) -> dict:
    """把某一段演示开在**看得见的控制台**里跑并截图。

    :param segment: ``"a"`` / ``"b"``。
    :return: 证据字典。
    """
    exit_file = os.path.join(
        tempfile.gettempdir(),
        "sentinel-v380-failover-{}-exitcode.txt".format(segment),
    )
    if os.path.exists(exit_file):
        os.remove(exit_file)

    merged = os.environ.copy()
    merged.update({
        "SENTINEL_DEMO_SEGMENT": segment,
        "SENTINEL_DEMO_EXITFILE": exit_file,
        "SENTINEL_DEMO_HOLD": "1",
    })
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--console-run"],
        creationflags=console_bridge.CREATE_NEW_CONSOLE,
        stdin=subprocess.PIPE,
        env=merged,
    )
    try:
        code = None
        for _ in range(120):
            time.sleep(0.5)
            if not os.path.exists(exit_file):
                continue
            try:
                with open(exit_file, "r", encoding="utf-8") as handle:
                    text = handle.read().strip()
                code = int(text.split("=", 1)[-1].strip())
            except (OSError, ValueError):
                code = None
            if code is not None:
                break
        print("  [{}] 子进程退出码：{}".format(segment, code))

        handle = None
        region = None
        for _attempt in range(12):
            handle = console_bridge.find_visible_window("SENTINEL-L2-FAILOVER")
            if handle is None:
                time.sleep(0.5)
                continue
            console_bridge.place_window(
                handle, CONSOLE_X, CONSOLE_Y, CONSOLE_W, CONSOLE_H
            )
            time.sleep(0.8)
            region = console_bridge.window_rect(handle)
            if region:
                break
        print("  [{}] 控制台窗口句柄：{}".format(segment, handle))
        print("  [{}] 控制台窗口矩形：{}".format(segment, region))
        if region is None:
            region = (CONSOLE_X, CONSOLE_Y, CONSOLE_W, CONSOLE_H)
        time.sleep(1.0)

        # 往外扩一圈再截 —— 窗口边框 / 阴影都在矩形之外。
        left = max(0, region[0] - 24)
        top = max(0, region[1] - 24)
        right = min(console_bridge.SCREEN_W, region[0] + region[2] + 24)
        bottom = min(console_bridge.SCREEN_H, region[1] + region[3] + 24)
        region = (left, top, right - left, bottom - top)

        path = os.path.join(
            ASSETS, "v380-failover-terminal-{}.png".format(segment)
        )
        ok = console_bridge.capture_screen(path, region)
        print("  [{}] 终端截图：{}（{}）".format(
            segment, path, "成功" if ok else "失败"
        ))
        return {
            "segment": segment,
            "exit_code": code,
            "window_handle": handle,
            "region": list(region),
            "screenshot": path,
            "screenshot_ok": ok,
        }
    finally:
        # 关掉 stdin 管道 —— 子进程读到 EOF 就自己退出、窗口关闭。
        if child.stdin is not None:
            try:
                child.stdin.close()
            except OSError:
                pass
        try:
            child.wait(timeout=15)
        except subprocess.SubprocessError:
            child.kill()


def _screenshot_run() -> int:
    """``--screenshot`` 模式：把演示开在**看得见的控制台**里并截图。

    :return: 退出码。
    """
    print("")
    print(_rule("="))
    print("  哨兵 v3.8.0 演示取证 —— L2 掉线热切 L3（真实控制台截图）")
    print(_rule("="))
    shots = [
        _capture_segment(segment) for segment in ("a", "b", "c")
    ]
    path = os.path.join(ASSETS, "v380-failover-screenshots.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(shots, handle, ensure_ascii=False, indent=2)
    print("")
    print("  截图清单 JSON：{}".format(path))
    ok = all(item["screenshot_ok"] for item in shots)
    print("  → {} 张截图{}".format(
        len(shots), "全部成功" if ok else "存在失败"
    ))
    return 0 if ok else 1


def main() -> int:
    """命令行入口。

    :return: 退出码（0 = 全部断言通过）。
    """
    console_bridge.make_utf8_stdout()

    if "--console-run" in sys.argv:
        return _console_run()

    if "--screenshot" in sys.argv:
        return _screenshot_run()

    evidence = _run_demo()
    path = _write_evidence(evidence)
    print("")
    print("  证据 JSON：{}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
