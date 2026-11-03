"""无头守护进程的本地 HTTP 接口（v5.0.0）。

只依赖标准库的 ``http.server`` —— **不引入 FastAPI / Uvicorn**。

为什么从 FastAPI 退回来
========================

哨兵 v5.0.0 起是**无头守护进程**：

* FastAPI + Uvicorn + SSE 那一整套是为「浏览器看板」准备的，
  而看板已经被剥掉了 —— 留着就是**纯粹的负资产**：
  多带几十 MB 依赖、常驻一个异步事件循环、多开一堆线程；
* 这个接口只服务**本机**的外部宿主（第三方 Agent Bridge 插件之类），
  四个端点、请求体都很小 —— 标准库足够。

四个端点
========

``GET  /api/health``
    活着吗？版本、会话数、有几路在休眠。

``POST /api/pre-check``
    外部宿主在 Agent 动手**之前**问一句：
    ``{"text": "……"}`` → ``{"action": "allow"|"block"}``。
    **纯 L1 规则，秒回，不调模型。**

``POST /api/attach``
    ``{"cwd": "…", "pid": 123, "log_path": "…"}``
    → 内核开始**后台静默**监听那一路。

``GET  /api/state``
    所有会话的快照（含各自的目标与休眠状态）。

报警出口
========

命中红线时**不弹窗**（没有 UI 了）：

* 往**终端**打一行；
* 配了 ``callback_url`` 就再 POST 一条**极简 JSON** 过去。

⚠️ **安全边界**：只绑 ``127.0.0.1``，且**没有任何鉴权** ——
它假定「同机的其他进程都是可信的」。
要跨机器用，请自己在前面加一层反代与鉴权。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .context import SessionRegistry, build_fingerprint
from .precheck import pre_check
from .worker import SessionWorker, adapter_for, build_session_rules

logger = logging.getLogger(__name__)

__all__ = [
    "DaemonApp",
    "DaemonHandler",
    "build_app",
    "serve",
]

#: 请求体最大字节数 —— 防止一个坏客户端把内存吃光。
MAX_BODY_BYTES = 1_000_000

#: 报警回调的超时（秒）。
CALLBACK_TIMEOUT = 5.0


def _resolve_goal(log_path: str, agent: str, cwd: str) -> str:
    """从日志首句里拔目标。

    :param log_path: 会话日志路径。
    :param agent: Agent 类型。
    :param cwd: 工作目录。
    :return: 目标文本；找不到就是空串（那一路进入 L2 休眠）。
    """
    if not log_path:
        return ""
    try:
        from src.radar import resolve_task_goal

        return resolve_task_goal(log_path, agent=agent, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 —— 拔不到目标不是致命错误
        logger.debug(
            "目标提取失败（该路将进入 L2 休眠）：%s: %s",
            type(exc).__name__,
            exc,
        )
        return ""


class DaemonApp:
    """无头守护进程的应用状态（不依赖任何 Web 框架）。"""

    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        config_path: Optional[str] = None,
        callback_url: str = "",
        version: str = "",
    ) -> None:
        """建应用。

        :param config: 已加载的配置字典。
        :param config_path: 配置文件路径。
        :param callback_url: 外部报警回调地址；空串表示不回调。
        :param version: 版本号（健康检查里回给调用方）。
        """
        self.config = config or {}
        self.config_path = config_path
        self.callback_url = str(callback_url or "").strip()
        self.version = str(version or "")
        #: 会话登记表。
        self.registry = SessionRegistry()
        #: 各路的后台线程。
        self._workers: Dict[str, SessionWorker] = {}
        self._lock = threading.Lock()

    # -- 健康 ------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        """健康检查。

        :return: 健康字典。
        """
        sessions = self.registry.snapshot()
        return {
            "status": "running",
            "version": self.version,
            "mode": "headless",
            "sessions": len(sessions),
            "l2_sleeping": sum(1 for item in sessions if item["l2_sleeping"]),
            "callback": bool(self.callback_url),
        }

    def state(self) -> Dict[str, Any]:
        """全部会话的快照。

        :return: 状态字典。
        """
        return {"sessions": self.registry.snapshot()}

    # -- 预检 ------------------------------------------------------------
    def pre_check(self, text: str) -> Dict[str, Any]:
        """L1 预检。

        :param text: 任务文本。
        :return: 判定字典。
        """
        return pre_check(
            text,
            config=self.config,
            config_path=self.config_path,
        )

    # -- 挂载 ------------------------------------------------------------
    def attach(
        self,
        *,
        cwd: str = "",
        pid: int = 0,
        log_path: str = "",
        agent: str = "",
        session_id: str = "",
    ) -> Dict[str, Any]:
        """挂载一路：开始后台静默监听。

        :param cwd: 工作目录。
        :param pid: 进程 ID。
        :param log_path: 会话日志路径（不给就按 cwd 找）。
        :param agent: Agent 类型。
        :param session_id: Agent 会话 ID。
        :return: 会话快照 + 挂载结果。
        """
        path = str(log_path or "").strip()
        if not path and cwd:
            path = self._guess_log_path(cwd)
        fingerprint = build_fingerprint(
            cwd=cwd, session_id=session_id, pid=pid
        )
        context = self.registry.get_or_create(
            fingerprint,
            session_id=session_id,
            cwd=cwd,
            pid=int(pid or 0),
            agent=agent,
            log_path=path,
            created_at=time.time(),
            updated_at=time.time(),
        )
        context.log_path = path
        context.cwd = str(cwd or context.cwd)
        context.pid = int(pid or context.pid)
        context.agent = str(agent or context.agent)
        # 这一路**自己**的规则引擎 —— 不共享。
        context.rules = build_session_rules(self.config, self.config_path)
        # 目标**自动绑定**：从日志首句拔；拔不到就是空串 → L2 休眠。
        context.bind_goal(_resolve_goal(path, context.agent, context.cwd))

        with self._lock:
            worker = self._workers.get(fingerprint)
            if worker is None:
                worker = SessionWorker(
                    context,
                    adapter_for(context.agent),
                    on_alert=self._on_alert,
                )
                self._workers[fingerprint] = worker
                worker.start()
        return context.to_dict()

    def _guess_log_path(self, cwd: str) -> str:
        """按工作目录猜会话日志路径。

        :param cwd: 工作目录。
        :return: 猜到的路径；找不到就是空串。
        """
        try:
            from src.radar import scan_agent_sessions

            for hit in scan_agent_sessions(cwd=cwd):
                if hit.attachable:
                    return hit.path
        except Exception as exc:  # noqa: BLE001 —— 猜不到不是致命错误
            logger.debug("猜日志路径失败：%s: %s", type(exc).__name__, exc)
        return ""

    # -- 报警 ------------------------------------------------------------
    def _on_alert(self, alert: Dict[str, Any]) -> None:
        """命中红线：终端 + （配了就）外部回调。

        :param alert: 报警字典。
        """
        print(
            "[SENTINEL][红线] {} | {} | {}".format(
                alert.get("agent") or alert.get("session_id") or "?",
                alert.get("rule_id") or "危险操作",
                alert.get("reason") or "",
            ),
            flush=True,
        )
        if self.callback_url:
            self.post_callback(alert)

    def post_callback(self, alert: Dict[str, Any]) -> bool:
        """往外部回调地址 POST 一条**极简**报警。

        :param alert: 报警字典。
        :return: 送达返回 ``True``。
        """
        url = self.callback_url
        if not url:
            return False
        payload = {
            "action": "block",
            "rule_id": alert.get("rule_id", ""),
            "reason": alert.get("reason", ""),
            "agent": alert.get("agent", ""),
            "session_id": alert.get("session_id", ""),
            "cwd": alert.get("cwd", ""),
            "pid": alert.get("pid", 0),
            "goal": alert.get("goal", ""),
            "ts": alert.get("ts", time.time()),
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=CALLBACK_TIMEOUT
            ) as response:
                response.read()
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("报警回调失败：%s: %s", type(exc).__name__, exc)
            return False

    def shutdown(self) -> None:
        """停掉所有后台线程。"""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()


class DaemonHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    #: 应用实例（由 :func:`build_app` 挂上去）。
    app: DaemonApp = None  # type: ignore[assignment]

    #: 不打访问日志 —— 无头守护进程的终端要留给告警。
    server_version = "SentinelDaemon/5.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """把访问日志降到 DEBUG —— 默认别刷屏。"""
        logger.debug("%s - %s", self.address_string(), format % args)

    # -- 读 --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
        """处理 GET。"""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/health":
            self._json(self.app.health())
            return
        if path == "/api/state":
            self._json(self.app.state())
            return
        self._json({"error": "not found", "path": path}, status=404)

    # -- 写 --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
        """处理 POST。"""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        payload = self._read_json()
        if payload is None:
            return
        if path == "/api/pre-check":
            text = str(payload.get("text") or payload.get("task") or "")
            self._json(self.app.pre_check(text))
            return
        if path == "/api/attach":
            self._json(
                self.app.attach(
                    cwd=str(payload.get("cwd") or ""),
                    pid=int(payload.get("pid") or 0),
                    log_path=str(payload.get("log_path") or ""),
                    agent=str(payload.get("agent") or ""),
                    session_id=str(payload.get("session_id") or ""),
                )
            )
            return
        self._json({"error": "not found", "path": path}, status=404)

    # -- 工具 ------------------------------------------------------------
    def _read_json(self) -> Optional[Dict[str, Any]]:
        """读请求体（JSON）。

        :return: 字典；读不出来是 ``None``（已经回过 400）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._json({"error": "payload too large"}, status=413)
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            self._json({"error": "bad json: {}".format(exc)}, status=400)
            return None
        if not isinstance(data, dict):
            self._json({"error": "body must be an object"}, status=400)
            return None
        return data

    def _json(self, payload: Any, status: int = 200) -> None:
        """回一个 JSON 响应。

        :param payload: 要 JSON 化的内容。
        :param status: HTTP 状态码。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


def build_app(
    *,
    config: Optional[Dict[str, Any]] = None,
    config_path: Optional[str] = None,
    callback_url: str = "",
    version: str = "",
) -> DaemonApp:
    """建一个应用实例。

    :param config: 已加载的配置字典。
    :param config_path: 配置文件路径。
    :param callback_url: 外部报警回调地址。
    :param version: 版本号。
    :return: 应用实例。
    """
    return DaemonApp(
        config=config,
        config_path=config_path,
        callback_url=callback_url,
        version=version,
    )


def serve(
    app: DaemonApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """跑起来（阻塞）。

    ⚠️ **只允许绑回环地址** —— 这个接口能挂载进程、能预检任务，
    本机任何进程都能调，绝不能暴露到网络上。

    :param app: 应用实例。
    :param host: 绑定地址。
    :param port: 绑定端口。
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            "只允许绑 127.0.0.1：这个接口能挂载进程，不能暴露到网络上。"
        )
    handler = type("BoundHandler", (DaemonHandler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    print(
        "[SENTINEL] 无头守护进程已启动  http://{}:{}/  "
        "（模式：headless；Ctrl+C 停止）".format(host, port),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SENTINEL] 已停止", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 退出码。
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="哨兵无头守护进程（v5.0.0）"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--config", default="")
    parser.add_argument("--callback-url", default="")
    args = parser.parse_args(list(argv or []))
    if args.host not in ("127.0.0.1", "localhost"):
        print("只允许绑 127.0.0.1。", flush=True)
        return 2

    config: Dict[str, Any] = {}
    config_path = args.config or None
    if config_path and os.path.exists(config_path):
        try:
            from src.config_loader import load_config

            config = load_config(config_path)
        except Exception as exc:  # noqa: BLE001 —— 配置读不到也要能跑
            print(
                "配置加载失败（用默认值继续）：{}: {}".format(
                    type(exc).__name__, exc
                ),
                flush=True,
            )
    try:
        from src import __version__ as version
    except Exception:  # noqa: BLE001 —— 版本读不到不影响服务
        version = ""

    app = build_app(
        config=config,
        config_path=config_path,
        callback_url=args.callback_url,
        version=version,
    )
    serve(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
