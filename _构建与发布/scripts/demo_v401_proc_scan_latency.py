"""进程树扫描「首发卡顿」治理验收演示（v4.0.1）。

这个脚本**只读** —— 它不碰任何被监控的日志，只做三件事：

1. **冷启动基线**：起一个**全新**的 Python 进程，直接扫一次进程树
   —— 复现 v4.0.0 实测的「首次 4.2 秒」；
2. **预热后基线**：再起一个**全新**的 Python 进程，先静默调一次
   :func:`src.proc_tree.find_bridge_hosts`（哨兵启动时干的事），
   然后再扫一次 —— 这就是用户点开雷达面板时的真实体验；
3. **超时熔断**：把扫描换成「慢的」，看接口是不是 1.5 秒就返回
   **部分结果**，而不是把用户堵住。

跑法::

    python _构建与发布/scripts/demo_v401_proc_scan_latency.py
    python _构建与发布/scripts/demo_v401_proc_scan_latency.py \
        --json 证据.json

⚠️ 为什么每段都要起**新进程**
==========================

Windows 上「第一次枚举几百个进程」贵的是**逐个打开进程句柄**，
而 psutil 会把句柄缓存住 —— 同一个进程里第二次扫就快了。
要复现「冷」与「热」的差别，只能用两个独立进程。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 —— 演示必须起真实进程
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 控制台可能是 GBK —— 先切 UTF-8，免得中文变成乱码。
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    try:
        _reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError, AttributeError):
        pass

#: 子进程里跑的「冷 / 热」测量脚本。
_CHILD = r'''
import json
import os
import sys
import time

ROOT = sys.argv[1]
MODE = sys.argv[2]
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.proc_tree import find_bridge_hosts, scan_process_tree

if MODE == "warm":
    t0 = time.perf_counter()
    hosts = find_bridge_hosts()
    warm_ms = (time.perf_counter() - t0) * 1000.0
else:
    hosts = []
    warm_ms = 0.0

t0 = time.perf_counter()
found = scan_process_tree()
scan_ms = (time.perf_counter() - t0) * 1000.0

print(json.dumps({
    "mode": MODE,
    "warm_ms": round(warm_ms, 1),
    "scan_ms": round(scan_ms, 1),
    "hosts": len(hosts),
    "found": len(found),
}))
'''


def _run_child(mode: str) -> Dict[str, Any]:
    """起一个全新进程测一次扫描耗时。

    :param mode: ``cold`` = 直接扫；``warm`` = 先预热再扫。
    :return: 子进程报回来的测量结果。
    """
    out = subprocess.run(  # noqa: S603 —— 演示必须起真实进程
        [sys.executable, "-c", _CHILD, ROOT, mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if out.returncode != 0:
        return {
            "mode": mode,
            "error": (out.stderr or "").strip()[-500:],
            "warm_ms": 0.0,
            "scan_ms": 0.0,
        }
    return json.loads((out.stdout or "").strip().splitlines()[-1])


def _demo_circuit_breaker() -> Dict[str, Any]:
    """演示「超时熔断」：扫描很慢时接口多久返回、返回了什么。

    :return: 熔断演示结果。
    """
    from src import proc_tree
    from src.core import session_server as gui_server

    gate = __import__("threading").Event()
    entered = __import__("threading").Event()

    def slow_scan(**kwargs: Any) -> List[Any]:
        """假扫描：先报一个结果，再卡住不动。"""
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(
                [
                    proc_tree.DiscoveredAgentProcess(
                        pid=777,
                        parent_pid=42,
                        host_pid=42,
                        depth=1,
                        agent="claude",
                        agent_label="Claude Code",
                        exe="C:/demo/claude.exe",
                        cmdline="claude.exe",
                        cwd="C:/demo",
                        confidence=0.95,
                    )
                ]
            )
        entered.set()
        gate.wait(timeout=30.0)
        return []

    original = proc_tree.scan_process_tree
    proc_tree.scan_process_tree = slow_scan  # type: ignore[assignment]
    try:
        started = time.perf_counter()
        rows = gui_server._scan_cloud_processes()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        state = gui_server._cloud_proc_scan_state()
    finally:
        gate.set()
        proc_tree.scan_process_tree = original  # type: ignore[assignment]
    return {
        "elapsed_ms": round(elapsed_ms, 1),
        "timeout_s": gui_server.CLOUD_PROC_SCAN_TIMEOUT,
        "rows": len(rows),
        "status": state["status"],
        "partial": state["partial"],
        "entered": entered.is_set(),
    }


class _Tee:
    """把 stdout 同时写到终端与一个 UTF-8 文件。

    为什么不让 shell 重定向来存终端实录？因为 Windows 控制台是
    GBK，重定向链路会把中文打成乱码 —— 证据文件必须是**脚本自己**
    以 UTF-8 写出来的才可信。

    :param stream: 原始 stdout。
    :param path: 目标文件路径。
    """

    def __init__(self, stream: Any, path: str) -> None:
        self.original = stream
        self._handle = open(path, "w", encoding="utf-8", newline="\n")

    def write(self, text: str) -> int:
        """写一份到终端、一份到文件。

        :param text: 待写文本。
        :return: 写出的字符数。
        """
        self._handle.write(text)
        return self.original.write(text)

    def flush(self) -> None:
        """把两层缓冲都冲掉。"""
        self._handle.flush()
        self.original.flush()

    def close(self) -> None:
        """关掉文件句柄。"""
        if not self._handle.closed:
            self._handle.close()


def main(argv: Optional[List[str]] = None) -> int:
    """主流程。

    :param argv: 命令行参数。
    :return: 进程退出码（0 = 演示通过）。
    """
    parser = argparse.ArgumentParser(
        description="v4.0.1 进程树扫描首发卡顿治理演示"
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="把证据同时写成 JSON 文件",
    )
    parser.add_argument(
        "--terminal",
        dest="terminal_path",
        default="",
        help="把终端实录写成 UTF-8 文本文件",
    )
    args = parser.parse_args(argv)

    tee: Optional[_Tee] = None
    if args.terminal_path:
        parent = os.path.dirname(os.path.abspath(args.terminal_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tee = _Tee(sys.stdout, args.terminal_path)
        sys.stdout = tee  # type: ignore[assignment]
    try:
        return _run_demo(args)
    finally:
        if tee is not None:
            sys.stdout = tee.original  # type: ignore[assignment]
            tee.close()


def _run_demo(args: argparse.Namespace) -> int:
    """跑演示本体。

    :param args: 已解析的命令行参数。
    :return: 进程退出码（0 = 演示通过）。
    """
    print("=" * 68)
    print("v4.0.1 进程树扫描「首发卡顿」治理 —— 验收演示")
    print("=" * 68)

    cold = _run_child("cold")
    print()
    print("【一】冷启动（没有预热）—— 用户点开雷达面板的第一下")
    print(
        "    首次全量扫描耗时：{:.0f} ms（{:.2f} 秒）".format(
            cold.get("scan_ms", 0.0),
            cold.get("scan_ms", 0.0) / 1000.0,
        )
    )
    print(
        "    认出宿主 {} 个，认出 Agent 进程 {} 个".format(
            cold.get("hosts", 0), cold.get("found", 0)
        )
    )

    warm = _run_child("warm")
    print()
    print("【二】预热后（哨兵启动时已静默调过 find_bridge_hosts）")
    print(
        "    预热（find_bridge_hosts）耗时：{:.0f} ms".format(
            warm.get("warm_ms", 0.0)
        )
    )
    print(
        "    预热后的首次全量扫描耗时：{:.0f} ms（{:.2f} 秒）".format(
            warm.get("scan_ms", 0.0),
            warm.get("scan_ms", 0.0) / 1000.0,
        )
    )
    print(
        "    认出宿主 {} 个，认出 Agent 进程 {} 个".format(
            warm.get("hosts", 0), warm.get("found", 0)
        )
    )

    breaker = _demo_circuit_breaker()
    print()
    print("【三】超时熔断（扫描故意变慢）")
    print(
        "    接口在 {:.0f} ms 返回（熔断阈值 {:.1f} 秒）".format(
            breaker["elapsed_ms"], breaker["timeout_s"]
        )
    )
    print(
        "    返回部分结果 {} 条，状态 = {}（partial = {}）".format(
            breaker["rows"], breaker["status"], breaker["partial"]
        )
    )

    cold_ms = float(cold.get("scan_ms", 0.0))
    warm_ms = float(warm.get("scan_ms", 0.0))
    speedup = (cold_ms / warm_ms) if warm_ms > 0 else 0.0
    print()
    print("=" * 68)
    print(
        "结论：冷启动 {:.0f} ms → 预热后 {:.0f} ms（快 {:.1f} 倍）；"
        "超时熔断在 {:.0f} ms 返回部分结果，**不阻塞主线程**。".format(
            cold_ms, warm_ms, speedup, breaker["elapsed_ms"]
        )
    )
    print("=" * 68)

    evidence = {
        "cold": cold,
        "warm": warm,
        "circuit_breaker": breaker,
        "speedup": round(speedup, 2),
    }
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("证据已写入：{}".format(args.json_path))
    if args.terminal_path:
        print("终端实录已写入：{}".format(args.terminal_path))

    ok = (
        warm_ms > 0
        and cold_ms > warm_ms
        and breaker["elapsed_ms"] <= breaker["timeout_s"] * 1000.0 + 500.0
        and breaker["partial"] is True
    )
    print("DEMO_EXIT={}".format(0 if ok else 1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
