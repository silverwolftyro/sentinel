"""故障注入 Mock Webhook 服务器 —— 端到端验证远程告警的失败处理。

为什么需要它
============
v1.0.0 的 Webhook 只做过 Mock 测试（用假的 ``requests`` 模块），
**从来没真的走过一次 HTTP**。但真实推送服务最要命的地方恰恰不在「报文长什么样」，
而在「它会**失败**」：

* 网络抖动 —— 偶发超时；
* 服务端抽风 —— 临时 500；
* 限流 —— 429。

所以这里起一个**真的 HTTP 服务器**，把��些麻烦**如实模拟**出来：

============================  ==========================================
路径                          行为
============================  ==========================================
``/ok``                       永远 200（带正常延迟）
``/flaky``                    前 N 次失败（默认 2 次 500），之后成功
``/always500``                永远 500
``/timeout``                  睡到比客户端超时还久 → 客户端超时
``/slow``                     睡指定毫秒数（模拟慢网络）
``/status/<code>``            返回指定状态码
============================  ==========================================

服务器会记录收到的**每一个**请求（路径 / 报文 / 时间），
用来断言「客户端到底重试了几次」。

用法::

    # 独立跑（供人工验证 / 手工联调）
    python webhook_fault_server.py --port 8787 --flaky-times 2

    # 当库用（测试与演练脚本）
    from webhook_fault_server import FaultWebhookServer
    server = FaultWebhookServer()
    server.start()
    print(server.base_url + "/ok")
    server.stop()
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

__all__ = ["FaultWebhookServer", "main"]


class _Handler(BaseHTTPRequestHandler):
    """处理注入故障的 HTTP 请求。"""

    # 静音：BaseHTTPRequestHandler 默认会把每个请求打到 stderr。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """不打印访问日志（我们自己记录）。"""

    def _record(self, body: bytes) -> Dict[str, Any]:
        """把这次请求记进服务器的台账。"""
        owner = self.server.owner  # type: ignore[attr-defined]
        payload: Dict[str, Any] = {
            "path": self.path,
            "method": self.command,
            "timestamp": time.time(),
            "body": body.decode("utf-8", errors="replace"),
        }
        try:
            payload["json"] = json.loads(payload["body"]) if payload["body"] else {}
        except (json.JSONDecodeError, ValueError):
            payload["json"] = {}
        with owner._lock:
            owner.received.append(payload)
            owner.path_counts[self.path] = (
                owner.path_counts.get(self.path, 0) + 1
            )
        return payload

    def _respond(self, status: int, body: Dict[str, Any]) -> None:
        """回一个 JSON 响应。"""
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
        """处理 POST（webhook 的投递方式）。"""
        owner = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        record = self._record(body)

        # --- 统一延迟（模拟慢网络）---
        if owner.latency > 0:
            time.sleep(owner.latency)

        path = record["path"].split("?")[0]

        if path.startswith("/status/"):
            try:
                code = int(path.rsplit("/", 1)[-1])
            except ValueError:
                code = 500
            self._respond(code, {"ok": False, "code": code})
            return

        if path == "/always500":
            self._respond(500, {"ok": False, "error": "服务端抽风"})
            return

        if path == "/timeout":
            # 睡到**比客户端超时还久** —— 客户端应当自己超时。
            time.sleep(owner.timeout_delay)
            self._respond(200, {"ok": True, "late": True})
            return

        if path == "/flaky":
            with owner._lock:
                attempt = owner.path_counts.get("/flaky", 0)
            if attempt <= owner.flaky_times:
                self._respond(500, {"ok": False, "attempt": attempt})
                return
            self._respond(200, {"ok": True, "attempt": attempt})
            return

        # /ok 与其它路径 —— 正常返回
        self._respond(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        """GET 返回一份统计（方便人工确认收到了几次）。"""
        owner = self.server.owner  # type: ignore[attr-defined]
        self._record(b"")
        self._respond(
            200,
            {
                "ok": True,
                "received": len(owner.received),
                "path_counts": dict(owner.path_counts),
            },
        )


class FaultWebhookServer:
    """可编程的故障注入 HTTP 服务器。

    :param host: 监听地址（默认只绑回环）。
    :param port: 监听端口；``0`` 表示让系统随机挑一个。
    :param latency: 每个请求的固定延迟（秒）—— 模拟慢网络。
    :param flaky_times: ``/flaky`` 前多少次返回 500。
    :param timeout_delay: ``/timeout`` 睡多久（秒）—— 要比客户端超时更久。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        latency: float = 0.0,
        flaky_times: int = 2,
        timeout_delay: float = 5.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.latency = max(float(latency or 0.0), 0.0)
        self.flaky_times = max(int(flaky_times or 0), 0)
        self.timeout_delay = max(float(timeout_delay or 0.0), 0.0)
        #: 收到的每一个请求。
        self.received: List[Dict[str, Any]] = []
        #: 每个路径被请求了几次。
        self.path_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def start(self) -> str:
        """启动服务器。

        :return: 基地址（如 ``http://127.0.0.1:8787``）。
        """
        if self._server is not None:
            return self.base_url
        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.owner = self  # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="fault-webhook",
            daemon=True,
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        """停掉服务器。"""
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        self._server = None
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        """服务器基地址。"""
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        """拼一个完整地址。

        :param path: 路径（如 ``/flaky``）。
        :return: 完整 URL。
        """
        return f"{self.base_url}{path}"

    def count(self, path: str) -> int:
        """某个路径被请求了几次。

        :param path: 路径。
        :return: 次数。
        """
        with self._lock:
            return int(self.path_counts.get(path, 0))

    def reset(self) -> None:
        """清空记录（复用同一个服务器做下一轮测试时用）。"""
        with self._lock:
            self.received.clear()
            self.path_counts.clear()

    def __enter__(self) -> "FaultWebhookServer":
        """支持 ``with`` 用法。"""
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """退出时停掉服务器。"""
        self.stop()


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表（默认取 ``sys.argv[1:]``）。
    :return: 退出码。
    """
    import argparse

    parser = argparse.ArgumentParser(description="故障注入 Mock Webhook 服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--latency", type=float, default=0.0, help="固定延迟（秒）")
    parser.add_argument(
        "--flaky-times", type=int, default=2, help="/flaky 前几次返回 500"
    )
    parser.add_argument(
        "--timeout-delay", type=float, default=5.0, help="/timeout 睡多久（秒）"
    )
    args = parser.parse_args(argv)

    server = FaultWebhookServer(
        host=args.host,
        port=args.port,
        latency=args.latency,
        flaky_times=args.flaky_times,
        timeout_delay=args.timeout_delay,
    )
    server.start()
    print(f"[Mock] 故障注入 Webhook 服务器已启动：{server.base_url}")
    print("[Mock] 可用路径：/ok、/flaky、/always500、/timeout、/slow、/status/<code>")
    print("[Mock] 按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[Mock] 已停止。")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover —— 命令行入口
    raise SystemExit(main())
