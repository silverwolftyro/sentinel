"""哨兵无头守护进程（v5.0.0）。

v5.0.0 起哨兵退居幕后：

* **没有前端** —— 面板、拖拽、手机推送、SSE 全部剥离；
* **会话级隔离** —— 目标锚点下沉到每一路会话，靠
  CWD / session_id / thread_id 精准分区；
* **轻量集成** —— 四个极简 HTTP 端点（``/api/health``、
  ``/api/pre-check``、``/api/attach``、``/api/state``），
  外部宿主拿它当「安检」与「挂起」的后端。

本包**只依赖标准库 + 哨兵自己的纯逻辑模块**，
不引入 FastAPI / Uvicorn。
"""

from __future__ import annotations

from .context import SessionContext, SessionRegistry, build_fingerprint
from .precheck import ACTION_ALLOW, ACTION_BLOCK, pre_check
from .server import DaemonApp, build_app, serve
from .worker import SessionWorker, adapter_for

__all__ = [
    "ACTION_ALLOW",
    "ACTION_BLOCK",
    "DaemonApp",
    "SessionContext",
    "SessionRegistry",
    "SessionWorker",
    "adapter_for",
    "build_app",
    "build_fingerprint",
    "pre_check",
    "serve",
]
