"""v3.3.0 验收演示 —— 引擎掉线红告警 + 时间盒拦截。

这个脚本**真的**做两件事（不是模拟）：

1. **拔掉本地引擎**：先确认 NInfer 活着 → 结束它 → 等心跳发现掉线
   → 打印红色告警 → 再把它拉起来 → 等心跳报恢复。
   ⚠️ 脚本**一定会**把引擎拉回来（``finally`` 里兜底）。
2. **喂一串「死磕」事件**：同一条命令在时间盒里反复出现
   → 看哨兵是不是真的打出 ``[CRITICAL] 严重跑偏``。

用法::

    python _构建与发布/scripts/demo_v330_watchdog_and_timebox.py
    python _构建与发布/scripts/demo_v330_watchdog_and_timebox.py --json 证据.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adapters.mcode import McodeAdapter  # noqa: E402
from src.core.engine import SentinelEngine  # noqa: E402
from src.core.engine_watchdog import EngineWatchdog  # noqa: E402
from src.core.timebox import TimeBoxDetector  # noqa: E402
from src.rules.basic import RuleEngine  # noqa: E402

#: 本机 NInfer 的默认地址。
DEFAULT_BASE_URL = "http://127.0.0.1:8888/v1"

#: 默认的引擎启动命令（与用户自己的启动面板一致）。
DEFAULT_ENGINE_CMD = [
    r"D:\Bonsai\ninfer-5070ti-win-x64-sm120a-v1.0.0.exe",
    r"D:\Bonsai\Ternary-Bonsai-2-27B-ninfer-v3.ninfer",
    "--max-context", "131072",
    "--kv-capacity", "131072",
    "--kv-dtype", "rk4v4-e8",
    "--gdn-state-fp16",
    "--prefill-chunk", "128",
    "--max-concurrency", "1",
    "--device-state-slots", "0",
    "--host-state-slots", "2",
    "--host-kv-mib", "8192",
    "--cuda-graph-allowance-mib", "320",
    "--max-shared-prefixes", "0",
    "--device-profile-path", r"D:\Bonsai\device-profiles.json",
    "--port", "8888",
]


def _say(text: str) -> None:
    """打印一行（演示脚本用 print 是允许的）。

    :param text: 要打印的内容。
    :return: 无。
    """
    print(text, flush=True)


def _engine_pids(base_url: str) -> List[int]:
    """找出正在跑的引擎进程。

    :param base_url: 引擎地址（目前只用来确认参数有效）。
    :return: PID 列表。
    """
    if "127.0.0.1" not in str(base_url) and "localhost" not in str(base_url):
        _say("  [警告] {} 看着不是本机地址，演示只处理本机引擎。".format(base_url))
    # ⚠️ 不能用 ``tasklist /FI "IMAGENAME eq ..."`` ——
    #    那个过滤条件只认**前 15 个字符**，
    #    而引擎的 exe 名比这长得多 → 一个都匹配不上。
    #    所以这里拉全量 CSV 再自己筛。
    output = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
    )
    text = (output.stdout or b"").decode("utf-8", errors="replace")
    pids: List[int] = []
    for line in text.splitlines():
        if "ninfer" not in line.lower():
            continue
        fields = [item.strip('"') for item in line.split('","')]
        if len(fields) >= 2 and fields[1].strip('"').isdigit():
            pids.append(int(fields[1].strip('"')))
    return pids


def _kill_engine() -> List[int]:
    """结束引擎进程（演示「掉线」）。

    :return: 被结束的 PID 列表。
    """
    pids = _engine_pids(DEFAULT_BASE_URL)
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True
        )
    return pids


def _start_engine(command: List[str]) -> None:
    """把引擎拉起来。

    :param command: 启动命令（参数列表）。
    :return: 无。
    """
    subprocess.Popen(
        command,
        cwd=os.path.dirname(command[0]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _wait_engine(base_url: str, timeout: float = 180.0) -> bool:
    """等引擎就绪。

    :param base_url: 引擎地址。
    :param timeout: 最长等待秒数。
    :return: 是否就绪。
    """
    import urllib.error
    import urllib.request

    url = "{}/models".format(base_url.rstrip("/"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if 200 <= int(resp.status) < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return False


def _demo_watchdog(base_url: str, engine_cmd: List[str]) -> Dict[str, Any]:
    """演示一：拔掉引擎 → 红告警 → 拉回来 → 恢复告警。

    :param base_url: 引擎地址。
    :param engine_cmd: 引擎启动命令。
    :return: 证据字典。
    """
    _say("=" * 68)
    _say("  演示一：本地引擎掉线 → 心跳看门狗的红告警")
    _say("=" * 68)

    alerts: List[Dict[str, Any]] = []
    watchdog = EngineWatchdog(
        base_url,
        model="bonsai2-27b",
        interval=5,
        timeout=5,
        prober=None,
    )

    def on_alert(alert: Dict[str, Any]) -> None:
        """把看门狗的告警**原样**打出来（终端红色）。"""
        alerts.append(alert)
        _say("")
        _say("  " + "!" * 64)
        for line in str(alert.get("reason") or "").splitlines():
            _say("  " + line)
        _say("  " + "!" * 64)

    watchdog.add_listener(on_alert)
    watchdog.start()

    evidence: Dict[str, Any] = {
        "base_url": base_url,
        "steps": [],
    }

    try:
        _say("  [1/4] 探活中（引擎现在应该活着）……")
        first = watchdog.check_once()
        _say(
            "        结果：{}（{}）".format(
                "通" if first.get("ok") else "不通",
                first.get("detail"),
            )
        )
        evidence["steps"].append(
            {"step": "before", "ok": bool(first.get("ok")),
             "detail": str(first.get("detail"))}
        )

        _say("  [2/4] **拔掉引擎**（模拟进程崩了 / 显存吃紧 / 被人手滑关掉）……")
        killed = _kill_engine()
        _say("        已结束引擎进程：{}".format(killed or "（没找到进程）"))
        evidence["steps"].append({"step": "killed", "pids": killed})

        offline_deadline = time.time() + 60
        while time.time() < offline_deadline:
            watchdog.check_once()
            if any(
                item.get("verdict") == "ENGINE_OFFLINE" for item in alerts
            ):
                break
            time.sleep(3)

        offline = [
            item for item in alerts if item.get("verdict") == "ENGINE_OFFLINE"
        ]
        evidence["steps"].append(
            {
                "step": "offline",
                "detected": bool(offline),
                "reason": offline[0].get("reason") if offline else "",
            }
        )

        _say("  [3/4] 把引擎拉回来……")
        _start_engine(engine_cmd)
        ready = _wait_engine(base_url)
        _say("        引擎就绪：{}".format("是" if ready else "否（超时）"))
        evidence["steps"].append({"step": "restarted", "ready": ready})

        recover_deadline = time.time() + 60
        while time.time() < recover_deadline:
            watchdog.check_once()
            if any(
                item.get("verdict") == "ENGINE_RECOVERED" for item in alerts
            ):
                break
            time.sleep(3)

        recovered = [
            item
            for item in alerts
            if item.get("verdict") == "ENGINE_RECOVERED"
        ]
        evidence["steps"].append(
            {
                "step": "recovered",
                "detected": bool(recovered),
                "reason": recovered[0].get("reason") if recovered else "",
            }
        )
    finally:
        watchdog.stop()
        # ⚠️ 兜底：不管上面怎么炸，都要把引擎拉回来。
        if not _wait_engine(base_url, timeout=5):
            _start_engine(engine_cmd)
            _wait_engine(base_url, timeout=180)

    evidence["alerts"] = alerts
    _say("")
    _say(
        "  结果：掉线告警={}，恢复告警={}".format(
            "有" if offline else "**没有**",
            "有" if recovered else "**没有**",
        )
    )
    return evidence


def _demo_timebox() -> Dict[str, Any]:
    """演示二：Agent 死磕同一件事 → 时间盒拦截。

    :return: 证据字典。
    """
    _say("")
    _say("=" * 68)
    _say("  演示二：Agent 死磕同一件事 → 时间盒拦截")
    _say("=" * 68)

    detector = TimeBoxDetector(
        window_minutes=0.1,   # 6 秒（演示用，别真等 15 分钟）
        min_repeats=3,
        categories=["dependency", "test"],
    )
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine({}),
        alert_log_path=os.path.join(
            os.environ.get("TEMP", "."), "sentinel_v330_demo_alert.log"
        ),
        console=True,
        timebox=detector,
    )

    alert = None
    for index in range(4):
        command = "pip install -r requirements.txt"
        _say("  [{}] Agent 又在装依赖：{}".format(index + 1, command))
        alert = engine.process_event(
            {
                "type": "tool_call",
                "source": "mcode",
                "tool": {
                    "name": "shell",
                    "command": command,
                    "arguments": {"command": command},
                },
            }
        )
        if alert:
            break
        time.sleep(4)

    engine.close()
    evidence = {
        "window_minutes": 0.1,
        "min_repeats": 3,
        "alert": alert,
    }
    _say("")
    if alert:
        _say("  结果：**时间盒被撞破了** —— 判定「严重跑偏」。")
    else:
        _say("  结果：**没有**触发时间盒告警（这不正常）。")
    return evidence


def main(argv: Optional[List[str]] = None) -> int:
    """脚本入口。

    :param argv: 命令行参数。
    :return: 退出码。
    """
    parser = argparse.ArgumentParser(description="v3.3.0 验收演示")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--json", default="")
    parser.add_argument(
        "--skip-engine",
        action="store_true",
        help="只演示时间盒，不拔引擎",
    )
    args = parser.parse_args(argv)

    evidence: Dict[str, Any] = {}
    if args.skip_engine:
        evidence["watchdog"] = {"skipped": True}
    else:
        evidence["watchdog"] = _demo_watchdog(args.base_url, DEFAULT_ENGINE_CMD)
    evidence["timebox"] = _demo_timebox()
    evidence["created_at"] = time.time()

    if args.json:
        path = os.path.abspath(args.json)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        _say("")
        _say("  证据已写入：{}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
