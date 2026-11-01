"""SSE 探针：连上哨兵看板的事件流，把收到的每一条事件打出来。

用途是**验证流式链路真的通了**：

    引擎(tail 常驻) → 告警日志 → 看板 tailer → SSE → 浏览器

这个脚本站在「浏览器」的位置上，用标准 HTTP 消费 ``/api/events``，
每收到一条就打印一行 JSON —— 于是「引擎产生的告警有没有实时推到前端」
这件事，就有了可以贴进报告的客观证据。

用法::

    python _构建与发布/scripts/probe_sse.py --url http://127.0.0.1:8131/api/events
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any, Dict, Optional


def _prepare_stdout() -> None:
    """让重定向 / 管道场景下的中文输出不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(
                    encoding="utf-8", errors="replace", line_buffering=True
                )
            except (ValueError, OSError, AttributeError):
                pass


def _consume(url: str, duration: float) -> int:
    """消费 SSE 流，直到超时。

    :param url: SSE 端点地址。
    :param duration: 最多监听多少秒。
    :return: 收到的告警条数。
    """
    deadline = time.monotonic() + duration
    alerts = 0
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=duration + 10) as response:
        for raw in response:
            if time.monotonic() > deadline:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            try:
                event: Dict[str, Any] = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = str(event.get("type") or "?")
            if kind == "alert":
                alerts += 1
                print(
                    "[SSE] 告警 {verdict} · {severity} · {category} · {reason}".format(
                        verdict=event.get("verdict"),
                        severity=event.get("severity"),
                        category=event.get("category"),
                        reason=event.get("reason"),
                    ),
                    flush=True,
                )
            else:
                print(f"[SSE] {kind} {payload[:160]}", flush=True)
    return alerts


def main(argv: Optional[list] = None) -> int:
    """命令行入口。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码。
    """
    parser = argparse.ArgumentParser(
        prog="probe_sse",
        description="连接哨兵看板的 SSE 事件流并打印收到的每一条事件",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/api/events",
        help="SSE 端点地址，默认 http://127.0.0.1:8000/api/events",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="监听多少秒后退出，默认 60",
    )
    args = parser.parse_args(argv)

    _prepare_stdout()
    print(f"[SSE] 连接 {args.url}，监听 {args.duration:.0f} 秒", flush=True)
    try:
        alerts = _consume(args.url, args.duration)
    except Exception as exc:  # noqa: BLE001 —— 探针失败只报告
        print(f"[SSE] 连接失败：{type(exc).__name__}: {exc}", flush=True)
        return 1
    print(f"[SSE] 结束，共收到 {alerts} 条告警", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
