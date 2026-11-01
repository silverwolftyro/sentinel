"""外部插件派发进程树雷达验收演示（v4.0.0）。

这个脚本**零侵入** —— 它不碰上游插件的任何代码，只是：

1. 先报**本机真实**的情况（有没有外部插件的桥接宿主在跑）；
2. 自己**造**一棵「桥接宿主 → Agent CLI」的进程树；
3. 用哨兵的雷达（:mod:`src.proc_tree`）把整棵树扫出来，
   认出宿主、认出 Agent、关联到工作目录下的日志；
4. 把结果打成人能读的表格，最后**把进程树拆干净**。

跑法::

    python _构建与发布/scripts/demo_proc_tree_radar.py
    python _构建与发布/scripts/demo_proc_tree_radar.py --json 证据.json

⚠️ 为什么要自己造一棵进程树
==========================

真实的外部插件派发是**同步**的：插件 ``dispatch()`` 用
``asyncio.run(run_agent(...))`` 阻塞等 Agent 跑完才返回，
Agent CLI 跑完即退。所以「碰巧抓到一个正在跑的」靠运气。

自己造一棵，才能**每次**都证明「父进程派生子进程」这条链路是通的。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess  # noqa: S404 —— 演示必须起真实进程
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.proc_tree import (  # noqa: E402
    DiscoveredAgentProcess,
    find_bridge_hosts,
    scan_process_tree,
    summarize_tree,
)

#: 假「桥接宿主」—— 拉起一个 Agent CLI 子进程，然后盯着它。
#:
#: ⚠️ 文件名里的 ``webui.server`` 就是**宿主特征** ——
#:    雷达靠它认出「这是外部插件的桥接宿主」。
_HOST_SCRIPT = '''"""假桥接宿主 —— 拉起一个 Agent CLI 子进程。"""
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, sys.argv[1]],
    cwd=sys.argv[2],
)
try:
    while child.poll() is None:
        time.sleep(0.2)
finally:
    child.terminate()
'''

#: 假「Agent CLI」—— 什么也不干，就活着。
_AGENT_SCRIPT = '''"""假 Agent CLI —— 什么也不干，就活着。"""
import time

time.sleep(300)
'''


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


def _kill_tree(proc: subprocess.Popen) -> None:
    """把一棵进程树拆干净（连子进程一起）。

    :param proc: 顶层进程。
    :return: 无。
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover —— 没装 psutil 就退化成杀一个
        killer = getattr(proc, "kill", None)
        if killer is not None:
            killer()
        return
    try:
        parent = psutil.Process(proc.pid)
    except Exception as exc:  # noqa: BLE001 —— 进程早没了就没事了
        logging.getLogger(__name__).debug(
            "取宿主进程 %s 失败：%s", proc.pid, exc
        )
        return
    victims: List[Any] = []
    try:
        victims = parent.children(recursive=True)
    except Exception as exc:  # noqa: BLE001 —— 读不到子进程就只杀自己
        logging.getLogger(__name__).debug("读子进程失败：%s", exc)
        victims = []
    for victim in victims:
        try:
            victim.terminate()
        except Exception as exc:  # noqa: BLE001 —— 已经死了就跳过
            logging.getLogger(__name__).debug(
                "终止子进程 %s 失败：%s", victim.pid, exc
            )
            continue
    try:
        parent.terminate()
    except Exception as exc:  # noqa: BLE001 —— 已经死了就跳过
        logging.getLogger(__name__).debug(
            "终止宿主 %s 失败：%s", parent.pid, exc
        )
        return
    _gone, alive = psutil.wait_procs(victims + [parent], timeout=3.0)
    for survivor in alive:
        try:
            survivor.kill()
        except Exception as exc:  # noqa: BLE001 —— 杀不掉就算了
            logging.getLogger(__name__).debug(
                "强杀 %s 失败：%s", survivor.pid, exc
            )
            continue


def _measure_scan() -> Tuple[float, int]:
    """量一次**真实**的雷达全量扫描耗时与进程数。

    ⚠️ 这里量的必须是 :func:`scan_process_tree` 本身 ——
    只数 ``psutil.pids()`` 是「进程数」，不是「扫描耗时」，
    两者差着一个数量级，不能混着说。

    :return: ``(扫描耗时秒, 进程数)``。
    """
    try:
        import psutil

        count = len(psutil.pids())
    except Exception as exc:  # noqa: BLE001 —— 量不出来就报 0
        logging.getLogger(__name__).debug("数进程失败：%s", exc)
        count = 0
    started = time.monotonic()
    scan_process_tree()
    return time.monotonic() - started, count


def _describe_item(item: DiscoveredAgentProcess) -> Dict[str, Any]:
    """把一条发现结果整理成可 JSON 化的字典。

    :param item: 发现结果。
    :return: 字典。
    """
    return {
        "pid": item.pid,
        "parent_pid": item.parent_pid,
        "host_pid": item.host_pid,
        "depth": item.depth,
        "agent": item.agent,
        "agent_label": item.agent_label,
        "cmdline": item.cmdline,
        "cwd": item.cwd,
        "confidence": item.confidence,
        "log_path": item.log_path,
        "attachable": item.attachable,
        "state": item.state,
        "matched_by": item.matched_by,
    }


# ---------------------------------------------------------------------------
# 演示
# ---------------------------------------------------------------------------
def _show_real_hosts() -> List[Dict[str, Any]]:
    """先报**本机真实**的桥接宿主。

    :return: 宿主字典列表。
    """
    _rule("第 1 步：本机真实的桥接宿主")
    hosts = find_bridge_hosts()
    if not hosts:
        _say(
            "本机没有正在跑的外部插件桥接宿主 —— "
            "这是正常的，哨兵不要求上游插件改任何代码。"
        )
        _say("（下面第 2 步会自己造一棵，证明雷达链路是通的。）")
        return []
    _say("扫到 %d 个桥接宿主：" % len(hosts))
    payload: List[Dict[str, Any]] = []
    for host in hosts:
        _say("  · pid=%-7d name=%s" % (host.pid, host.name))
        _say("      exe  = %s" % host.exe)
        _say("      cwd  = %s" % host.cwd)
        _say("      命令 = %s" % host.cmdline)
        payload.append(
            {
                "pid": host.pid,
                "name": host.name,
                "exe": host.exe,
                "cwd": host.cwd,
                "cmdline": host.cmdline,
                "depth": host.depth,
            }
        )
    return payload


def _spawn_tree(workdir: str) -> Tuple[subprocess.Popen, str]:
    """造一棵「宿主 → Agent CLI」进程树。

    :param workdir: 进程树的工作目录（Agent 的日志也放这儿）。
    :return: ``(宿主进程, Agent 日志路径)``。
    """
    host_script = os.path.join(workdir, "webui.server.py")
    agent_script = os.path.join(workdir, "claude.py")
    log_path = os.path.join(workdir, "session.jsonl")
    with open(host_script, "w", encoding="utf-8") as handle:
        handle.write(_HOST_SCRIPT)
    with open(agent_script, "w", encoding="utf-8") as handle:
        handle.write(_AGENT_SCRIPT)
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write('{"type": "text", "text": "hi"}\n')

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [
            sys.executable,
            host_script,
            agent_script,
            workdir,
        ],
        cwd=workdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return proc, log_path


def _wait_for_tree(
    host_pid: int,
    *,
    timeout: float = 40.0,
    interval: float = 0.4,
) -> List[DiscoveredAgentProcess]:
    """反复扫，直到把宿主派生的 Agent 进程认出来。

    :param host_pid: 宿主进程 id。
    :param timeout: 总超时（秒）。
    :param interval: 两次扫描之间的间隔（秒）。
    :return: 扫到的发现结果列表。
    """
    deadline = time.monotonic() + timeout
    last: List[DiscoveredAgentProcess] = []
    while time.monotonic() < deadline:
        last = scan_process_tree()
        for item in last:
            if item.host_pid == host_pid and item.agent:
                return last
        time.sleep(interval)
    return last


def _show_scan(
    rows: List[DiscoveredAgentProcess],
    host_pid: int,
    log_path: str,
) -> List[Dict[str, Any]]:
    """把扫描结果打成人能读的表格。

    :param rows: 发现结果列表。
    :param host_pid: 宿主进程 id。
    :param log_path: Agent 日志路径。
    :return: 每条结果的可 JSON 化字典。
    """
    _rule("第 3 步：雷达扫到的进程树")
    if not rows:
        _say("雷达没扫到任何东西 —— 这是失败，不能算通过。")
        return []

    _say("%-8s %-8s %-12s %-10s %s" % (
        "pid", "父pid", "Agent", "认出的方式", "工作目录"))
    _say("-" * 72)
    payload: List[Dict[str, Any]] = []
    for item in rows:
        _say(
            "%-8d %-8d %-12s %-10s %s"
            % (
                item.pid,
                item.parent_pid,
                item.agent or "（认不出）",
                item.matched_by,
                item.cwd,
            )
        )
        _say("         命令行：%s" % item.cmdline)
        payload.append(_describe_item(item))

    _say("")
    _say("宿主 pid=%d 派生出 %d 个 Agent 进程："
         % (host_pid, len([r for r in rows if r.host_pid == host_pid])))
    for item in rows:
        if item.host_pid != host_pid:
            continue
        _say(
            "  · %s（%s） pid=%d，父进程=%d，第 %d 层"
            % (
                item.agent_label or item.agent,
                item.agent,
                item.pid,
                item.parent_pid,
                item.depth,
            )
        )
        _say("      关联日志：%s" % (item.log_path or "（没找到）"))
        _say(
            "      能不能挂载：%s"
            % ("能（关联到了真实存在的日志）" if item.attachable else "不能")
        )
    return payload


def _show_summary(rows: List[DiscoveredAgentProcess]) -> None:
    """打印雷达自带的摘要。

    :param rows: 发现结果列表。
    :return: 无。
    """
    _rule("第 4 步：雷达摘要（看板用的就是这段话）")
    _say(summarize_tree(rows))


def _main() -> Tuple[int, Dict[str, Any]]:
    """跑一遍演示。

    :return: ``(进程退出码, 证据字典)``。
    """
    _force_utf8_stdout()
    logging.basicConfig(level=logging.WARNING)

    _rule("外部插件派发进程树雷达 —— v4.0.0 验收演示")
    _say("Python  : %s" % sys.version.split()[0])
    try:
        import psutil

        _say("psutil  : %s" % psutil.__version__)
    except ImportError:
        _say("psutil  : 未安装（雷达会扫不到任何东西）")
    cost, count = _measure_scan()
    _say("本机进程数：%d" % count)
    _say(
        "首次全量扫描耗时：%.0f ms"
        "（Windows 上第一次要几秒，psutil 缓存住之后只要几十毫秒）"
        % (cost * 1000)
    )

    evidence: Dict[str, Any] = {
        "version": "v4.0.0",
        "feature": "外部插件派发进程树雷达",
        "python": sys.version.split()[0],
        "process_count": count,
    }
    real_hosts = _show_real_hosts()
    evidence["real_hosts"] = real_hosts

    workdir = tempfile.mkdtemp(prefix="sentinel-proc-tree-demo-")
    host_proc: Optional[subprocess.Popen] = None
    try:
        _rule("第 2 步：造一棵「桥接宿主 → Agent CLI」进程树")
        host_proc, log_path = _spawn_tree(workdir)
        _say("宿主进程 pid=%d（脚本名 webui.server.py —— 宿主特征）"
             % host_proc.pid)
        _say("它会派生一个 claude.py 子进程，工作目录 %s" % workdir)
        _say("Agent 日志：%s" % log_path)
        _say("")
        evidence["host_pid"] = host_proc.pid
        evidence["agent_log_path"] = log_path

        warm_started = time.monotonic()
        rows = _wait_for_tree(host_proc.pid)
        _say(
            "本次扫描耗时：%.0f ms（psutil 缓存已热）"
            % ((time.monotonic() - warm_started) * 1000)
        )
        payload = _show_scan(rows, host_proc.pid, log_path)
        evidence["discovered"] = payload
        _show_summary(rows)

        _rule("验收结论")
        matched = [
            row
            for row in rows
            if row.host_pid == host_proc.pid and row.agent == "claude"
        ]
        if not matched:
            _say("❌ 雷达没有扫出宿主派生的 Agent 进程 —— 不通过。")
            evidence["passed"] = False
            return 1, evidence
        item = matched[0]
        _say("✅ 扫出「父进程派生子进程」：")
        _say("   宿主 pid=%d → Agent pid=%d（父进程=%d，第 %d 层）"
             % (host_proc.pid, item.pid, item.parent_pid, item.depth))
        _say("   认出方式：%s（置信度 %.2f）" % (item.matched_by, item.confidence))
        _say("   关联日志：%s" % item.log_path)
        _say("   可挂载：%s" % item.attachable)
        if not item.attachable:
            _say("⚠️ 没有关联到真实日志，按红线 R7 不允许挂载 —— "
                 "这条不满足验收要求。")
            evidence["passed"] = False
            return 1, evidence
        if os.path.normcase(item.log_path) != os.path.normcase(log_path):
            _say("⚠️ 关联到的日志不是我们造的那个 —— 不通过。")
            evidence["passed"] = False
            return 1, evidence
        _say("✅ 一键挂载的路径有效（且文件真实存在）。")
        evidence["passed"] = True
        return 0, evidence
    finally:
        if host_proc is not None:
            _kill_tree(host_proc)
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
        description="外部插件派发进程树雷达验收演示（v4.0.0）"
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
