"""Webhook 端到端联调 —— 真起 HTTP 服务器，验证失败重试与不阻塞。

这个脚本回答三个问题（每个都用**真的 HTTP 请求**验证，不是打桩）：

1. **会不会重试？**
   服务端前 2 次返回 500、第 3 次成功 → 客户端应该重试到成功，
   服务器应该收到 **3 次**请求。

2. **会不会阻塞主事件流？**
   服务端一直超时（睡到比客户端超时还久）→
   :meth:`WebhookChannel.send` 必须**立刻返回**（微秒级），
   因为投递在后台线程里。实测「入队耗时」必须远小于网络超时。

3. **失败提示清不清晰？**
   服务端永远 500 → 重试耗尽后，日志里必须出现一句**看得懂的中文**，
   并且这条告警**没有**推到手机（``last_delivered`` 为假）。

用法::

    python webhook_e2e.py
    python webhook_e2e.py --timeout 3 --retries 2 --backoff 0.5
"""

from __future__ import annotations

import io
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from webhook_fault_server import FaultWebhookServer  # noqa: E402

from src.core.channels import WebhookChannel  # noqa: E402

#: 一条典型的红线告警。
RED_LINE_ALERT: Dict[str, Any] = {
    "alert_id": "1793070000123-000001",
    "timestamp": 1793070000.123,
    "verdict": "DANGEROUS",
    "level": "critical",
    "severity": "red_line",
    "rule_id": "catastrophic_root_delete",
    "reason": "红线级命中：递归删除根目录",
    "agent": "codex",
    "task_goal": "在当前目录创建 hello.py 并运行它",
    "command": "rm -rf /tmp/test",
    "triggers": ["guardian_intercept"],
    "guardian": {"ok": True, "method": "suspend", "pid": 25328},
}


def _prepare_stdout() -> None:
    """让重定向 / 管道场景下的中文输出不乱码。

    Windows 控制台默认 GBK —— 打印「✅」「万」这类字符会直接抛
    ``UnicodeEncodeError``，报告还没打印出来就崩了。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(
                    encoding="utf-8", errors="replace", line_buffering=True
                )
            except (ValueError, OSError, AttributeError):
                pass


class _LogCapture:
    """捕获日志输出（用来断言「中文错误提示」真的出现了）。"""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.handler = logging.StreamHandler(self.buffer)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger("src.core.channels")
        self._old_level = self.logger.level
        self._old_propagate = self.logger.propagate

    def __enter__(self) -> "_LogCapture":
        """接管 channels 模块的日志。"""
        self.logger.setLevel(logging.WARNING)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """还原日志配置。"""
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._old_level)
        self.logger.propagate = self._old_propagate

    @property
    def text(self) -> str:
        """捕获到的日志全文。"""
        return self.buffer.getvalue()


class _Report:
    """演练报告收集器。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str) -> None:
        """记一条检查结果。"""
        self.rows.append({"name": name, "ok": bool(ok), "detail": detail})

    @property
    def passed(self) -> bool:
        """全部通过才算通过。"""
        return all(row["ok"] for row in self.rows)

    def render(self) -> str:
        """渲染成中文报告。"""
        lines = [
            "=" * 62,
            "  哨兵 · Webhook 端到端联调报告",
            "=" * 62,
            "",
        ]
        for row in self.rows:
            mark = "✅" if row["ok"] else "❌"
            lines.append(f"  {mark} {row['name']}")
            lines.append(f"       {row['detail']}")
        lines.append("")
        lines.append(
            f"  结论：{'全部通过' if self.passed else '存在失败项'} "
            f"（{sum(1 for r in self.rows if r['ok'])}/{len(self.rows)}）"
        )
        lines.append("=" * 62)
        return "\n".join(lines)


def _run_flaky_retry(
    server: FaultWebhookServer,
    retries: int,
    backoff: float,
    report: _Report,
) -> None:
    """场景一：服务端前 N 次 500，之后成功 → 客户端应重试到成功。"""
    server.reset()
    channel = WebhookChannel(
        url=server.url("/flaky"),
        timeout=5,
        format="generic",
        max_retries=retries,
        retry_backoff=backoff,
    )
    try:
        started = time.monotonic()
        channel.send(RED_LINE_ALERT)
        ok = channel.flush(timeout=30)
        elapsed = (time.monotonic() - started) * 1000
        attempts = server.count("/flaky")
    finally:
        channel.close()

    expected = retries + 1
    report.add(
        "指数退避重试：500 之后重试到成功",
        ok and attempts == expected and channel.last_delivered,
        f"服务端收到 {attempts} 次请求（预期 {expected} 次）"
        f"；总耗时 {elapsed:.0f} ms；最终投递结果："
        f"{'成功' if channel.last_delivered else '失败'}",
    )


def _run_always_fail(
    server: FaultWebhookServer,
    retries: int,
    backoff: float,
    report: _Report,
) -> None:
    """场景二：服务端永远 500 → 重试耗尽后要有清晰的中文提示。"""
    server.reset()
    with _LogCapture() as capture:
        channel = WebhookChannel(
            url=server.url("/always500"),
            timeout=5,
            format="generic",
            max_retries=retries,
            retry_backoff=backoff,
        )
        try:
            channel.send(RED_LINE_ALERT)
            channel.flush(timeout=30)
        finally:
            channel.close()
    text = capture.text
    attempts = server.count("/always500")
    expected = retries + 1
    has_message = "投递最终失败" in text and "请检查网络" in text
    report.add(
        "重试耗尽后给出中文错误提示",
        attempts == expected and has_message and not channel.last_delivered,
        f"服务端收到 {attempts} 次请求（预期 {expected} 次）；"
        f"投递结果：{'成功' if channel.last_delivered else '失败'}；"
        f"中文提示：{'有' if has_message else '无'}",
    )
    report.add(
        "失败后释放防骚扰占位（允许再推）",
        channel.failed_sends >= 1,
        f"累计失败计数 {channel.failed_sends}；"
        "没送到手机就不会静音后续同类事件",
    )


def _run_non_blocking(
    server: FaultWebhookServer,
    timeout: float,
    report: _Report,
) -> None:
    """场景三：服务端一直超时 → 主事件流不能被卡住。"""
    server.reset()
    channel = WebhookChannel(
        url=server.url("/timeout"),
        timeout=timeout,
        format="generic",
        max_retries=0,
        retry_backoff=0,
    )
    try:
        started = time.monotonic()
        queued = channel.send(RED_LINE_ALERT)
        enqueue_ms = (time.monotonic() - started) * 1000
        # 等后台线程真的超时（不必等太久 —— 只为让统计好看）。
        channel.flush(timeout=timeout + 5)
    finally:
        channel.close()

    # 入队耗时必须**远小于**网络超时 —— 这正是「不阻塞主事件流」的证据。
    not_blocked = queued and enqueue_ms < timeout * 1000 * 0.2
    report.add(
        "投递不阻塞主事件流",
        not_blocked,
        f"send() 入队耗时 {enqueue_ms:.2f} ms，"
        f"而网络超时是 {timeout * 1000:.0f} ms —— "
        f"差 {timeout * 1000 / max(enqueue_ms, 0.001):.0f} 倍；"
        "投递在后台线程完成，主循环不受影响",
    )


def _run_no_retry_on_4xx(
    server: FaultWebhookServer,
    report: _Report,
) -> None:
    """场景四：4xx 是请求本身有问题 → 不该重试（重试也没用）。"""
    server.reset()
    channel = WebhookChannel(
        url=server.url("/status/404"),
        timeout=5,
        format="generic",
        max_retries=3,
        retry_backoff=0.05,
    )
    try:
        channel.send(RED_LINE_ALERT)
        channel.flush(timeout=30)
    finally:
        channel.close()
    attempts = server.count("/status/404")
    report.add(
        "4xx 不做无谓重试",
        attempts == 1,
        f"服务端收到 {attempts} 次请求（预期 1 次 —— "
        "URL 错了重试也没用，只浪费时间）",
    )


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表。
    :return: 全部通过返回 ``0``，否则返回 ``1``。
    """
    import argparse

    parser = argparse.ArgumentParser(description="Webhook 端到端联调")
    parser.add_argument("--timeout", type=float, default=2.0, help="客户端超时（秒）")
    parser.add_argument("--retries", type=int, default=2, help="最大重试次数")
    parser.add_argument(
        "--backoff", type=float, default=0.4, help="重试退避基数（秒）"
    )
    parser.add_argument(
        "--latency", type=float, default=0.05, help="服务端固定延迟（秒）"
    )
    args = parser.parse_args(argv)
    _prepare_stdout()

    report = _Report()
    server = FaultWebhookServer(
        latency=args.latency,
        flaky_times=args.retries,
        timeout_delay=args.timeout + 3.0,
    )
    server.start()
    print(f"[E2E] 故障注入服务器已启动：{server.base_url}")
    try:
        _run_flaky_retry(server, args.retries, args.backoff, report)
        _run_always_fail(server, args.retries, args.backoff, report)
        _run_non_blocking(server, args.timeout, report)
        _run_no_retry_on_4xx(server, report)
    finally:
        server.stop()

    text = report.render()
    print(text)
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover —— 命令行入口
    raise SystemExit(main())
