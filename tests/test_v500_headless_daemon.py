"""v5.0.0 回归：无头守护进程、会话级隔离与无目标休眠。

这一版的核心变化
================

* **面板整体剥离** —— 哨兵退居幕后，成为**无头守护进程**；
* **会话级隔离** —— 目标锚点下沉到每一路会话，
  靠 CWD / session_id / thread_id 精准分区；
* **轻量集成** —— 四个极简 HTTP 端点（只用标准库 ``http.server``）。

本文件盯四件事
==============

1. **隔离**：两路不同 CWD 的会话，目标**绝不串台**；
2. **休眠**：没有目标的会话，**L2 不调模型**（只留 L1）；
3. **接口**：``/api/health`` ``/api/pre-check`` ``/api/attach``
   ``/api/state`` 四个端点 behaving 正确；
4. **队列**：多路并发时 P0（红线）**插队**到最前。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.daemon import SessionRegistry, build_app, build_fingerprint, pre_check
from src.daemon.context import SessionContext
from src.daemon.server import DaemonApp

#: 仓库根目录。
ROOT_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 会话级隔离
# ---------------------------------------------------------------------------
def test_不同cwd得到不同指纹() -> None:
    """CWD 不同 → 指纹不同 → 天然分区。"""
    first = build_fingerprint(cwd="D:/proj/alpha", session_id="s1")
    second = build_fingerprint(cwd="D:/proj/beta", session_id="s2")
    assert first != second
    assert "alpha" in first
    assert "beta" in second


def test_同cwd同session得到同一指纹() -> None:
    """指纹必须**稳定** —— 不稳定会把同一路劈成两半。"""
    first = build_fingerprint(cwd="D:/proj/alpha", session_id="s1")
    second = build_fingerprint(cwd="D:/proj/alpha", session_id="s1")
    assert first == second


def test_两路会话的目标互不串台() -> None:
    """**核心断言** —— A 路的目标不许跑到 B 路上。"""
    registry = SessionRegistry()
    first = registry.get_or_create(
        build_fingerprint(cwd="D:/p/a", session_id="a"), cwd="D:/p/a"
    )
    second = registry.get_or_create(
        build_fingerprint(cwd="D:/p/b", session_id="b"), cwd="D:/p/b"
    )
    first.bind_goal("重构支付模块")

    assert first.goal == "重构支付模块"
    assert second.goal == ""
    assert first is not second


def test_同一指纹重复取不会重建() -> None:
    """重建会把已有滑窗清空 —— 那一路的历史就没了。"""
    registry = SessionRegistry()
    key = build_fingerprint(cwd="D:/p/a")
    first = registry.get_or_create(key, cwd="D:/p/a")
    first.bind_goal("原目标")
    second = registry.get_or_create(key, cwd="D:/p/a")
    assert first is second
    assert second.goal == "原目标"


def test_登记表能列出与删除() -> None:
    """登记表的基本操作。"""
    registry = SessionRegistry()
    key = build_fingerprint(cwd="D:/p/a")
    registry.get_or_create(key)
    assert len(registry.list()) == 1
    assert registry.get(key) is not None
    assert registry.remove(key) is True
    assert registry.get(key) is None
    assert registry.remove(key) is False


def test_分路追踪器目标各自独立() -> None:
    """``sibling()`` 造出来的新追踪器**有自己的目标**。

    这就是「多路会话目标不串台」的引擎级原语 ——
    每一路的 L2 Prompt 注入的是**它自己**的目标。
    """
    from src.core.engine import IntentTracker

    base = IntentTracker(goal="重构支付模块")
    other = base.sibling(goal="写一份部署文档")

    assert base.goal == "重构支付模块"
    assert other.goal == "写一份部署文档"
    assert base is not other
    other.goal = "改成别的目标"
    assert base.goal == "重构支付模块"


# ---------------------------------------------------------------------------
# 无目标休眠
# ---------------------------------------------------------------------------
def test_没目标就休眠() -> None:
    """没有目标就没有「偏离」可言 —— 这一路 L2 休眠。"""
    context = SessionContext(fingerprint="fp")
    assert context.l2_sleeping is True
    context.bind_goal("做点什么")
    assert context.l2_sleeping is False
    context.bind_goal("")
    assert context.l2_sleeping is True


def test_休眠状态出现在快照里() -> None:
    """休眠是**可观测**的 —— 外部要能看见哪几路在睡。"""
    context = SessionContext(fingerprint="fp", agent="mcode")
    snapshot = context.to_dict()
    assert snapshot["l2_sleeping"] is True
    assert snapshot["goal"] == ""
    assert snapshot["agent"] == "mcode"




# ---------------------------------------------------------------------------
# L1 预检
# ---------------------------------------------------------------------------
def test_预检放行普通任务() -> None:
    """普通活儿得放行 —— 预检拦太狠就没人用了。"""
    result = pre_check("把 README 里的安装说明改清楚一点")
    assert result["action"] == "allow"
    assert result["degraded"] is False


def test_预检拦下灾难级命令() -> None:
    """红线级命令必须拦下。"""
    result = pre_check("rm -rf / --no-preserve-root")
    assert result["action"] == "block"
    assert result["rule_id"]
    assert result["reason"]


def test_预检失败时放行() -> None:
    """预检自己出错时**必须放行** —— 宁可信其无，不可误杀。"""

    class _Boom:
        def check_event(self, _event: Any) -> Dict[str, Any]:
            raise RuntimeError("炸了")

    result = pre_check("随便什么", rules=_Boom())
    assert result["action"] == "allow"
    assert result["degraded"] is True


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------
class _Server:
    """在后台线程里跑一个守护服务。"""

    def __init__(self, app: DaemonApp, port: int) -> None:
        """建服务。"""
        from http.server import ThreadingHTTPServer

        from src.daemon.server import DaemonHandler

        handler = type("BoundHandler", (DaemonHandler,), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = port
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    def __enter__(self) -> "_Server":
        """启动。"""
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        """停掉。"""
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        """拼 URL。"""
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def get(self, path: str) -> Dict[str, Any]:
        """发 GET。"""
        with urllib.request.urlopen(self.url(path), timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发 POST。"""
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(scope="module")
def server() -> Any:
    """起一个守护服务（模块级，全套测试共用一个）。"""
    app = build_app(version="5.0.0")
    with _Server(app, 18787) as instance:
        yield instance


def test_健康检查(server: Any) -> None:
    """``/api/health`` 得报出「活着 + 什么模式 + 几路在睡」。"""
    payload = server.get("/api/health")
    assert payload["status"] == "running"
    assert payload["mode"] == "headless"
    assert payload["version"] == "5.0.0"
    assert "sessions" in payload
    assert "l2_sleeping" in payload


def test_预检接口(server: Any) -> None:
    """``/api/pre-check`` 外部宿主问一句，秒回 allow/block。"""
    assert server.post("/api/pre-check", {"text": "改个 README"})[
        "action"
    ] == "allow"
    blocked = server.post(
        "/api/pre-check", {"text": "rm -rf / --no-preserve-root"}
    )
    assert blocked["action"] == "block"


def test_挂载接口建立会话(server: Any) -> None:
    """``/api/attach`` 建一路会话，并报出它自己的目标与休眠状态。"""
    payload = server.post(
        "/api/attach",
        {"cwd": "D:/projects/alpha", "pid": 4321, "agent": "mcode"},
    )
    assert payload["cwd"] == "D:/projects/alpha"
    assert payload["pid"] == 4321
    assert payload["agent"] == "mcode"
    assert "l2_sleeping" in payload
    assert payload["fingerprint"]


def test_两路挂载的目标各自独立(server: Any) -> None:
    """**验收项** —— 同一时刻两路不同 CWD，状态各自独立。"""
    first = server.post(
        "/api/attach",
        {"cwd": "D:/projects/one", "pid": 1001, "agent": "mcode"},
    )
    second = server.post(
        "/api/attach",
        {"cwd": "D:/projects/two", "pid": 1002, "agent": "codex"},
    )
    assert first["fingerprint"] != second["fingerprint"]

    state = server.get("/api/state")
    sessions = state["sessions"]
    assert len(sessions) >= 2
    by_cwd = {item["cwd"]: item for item in sessions}
    assert by_cwd["D:/projects/one"]["pid"] == 1001
    assert by_cwd["D:/projects/two"]["pid"] == 1002
    assert by_cwd["D:/projects/one"]["agent"] == "mcode"
    assert by_cwd["D:/projects/two"]["agent"] == "codex"


def test_未知路径返回404(server: Any) -> None:
    """不认识的路径得回 404，不能静默成功。"""
    with pytest.raises(urllib.error.HTTPError) as info:
        server.get("/api/no-such-endpoint")
    assert info.value.code == 404


def test_坏JSON返回400(server: Any) -> None:
    """坏请求体得回 400。"""
    request = urllib.request.Request(
        server.url("/api/pre-check"),
        data="{ 这不是 JSON".encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=10)
    assert info.value.code == 400


# ---------------------------------------------------------------------------
# 任务四：并发与队列
# ---------------------------------------------------------------------------
def test_红线任务插队到最前() -> None:
    """**P0（红线）必须插到 P1/P2 前面** —— 多路并发时也一样。

    做法：先投一个**会卡住**的 P2 占住唯一的工作线程，
    再依次投 P2、P1、P0；放开卡住的那个之后，
    出来的顺序必须是 ``卡住的 → P0 → P1 → P2``。
    """
    from src.core.priority_queue import (
        PRIORITY_P0,
        PRIORITY_P1,
        PRIORITY_P2,
        PriorityTaskQueue,
    )

    order: List[int] = []
    gate = threading.Event()

    def _handler(payload: Dict[str, Any]) -> None:
        if payload.get("block"):
            gate.wait(5)
        order.append(int(payload["n"]))

    queue = PriorityTaskQueue(_handler)
    try:
        queue.submit("blocker", {"n": -1, "block": True}, PRIORITY_P2)
        time.sleep(0.3)
        queue.submit("p2", {"n": 2}, PRIORITY_P2)
        queue.submit("p1", {"n": 1}, PRIORITY_P1)
        queue.submit("p0", {"n": 0}, PRIORITY_P0)
        gate.set()
        queue.drain(timeout=15)
    finally:
        queue.stop()

    assert order[0] == -1, "卡住的那个应当先跑完"
    assert order[1:] == [0, 1, 2], "P0 必须插到最前：{}".format(order)


def test_同档内先来先服务() -> None:
    """同档位不插队 —— 顺序必须是投递顺序。"""
    from src.core.priority_queue import PRIORITY_P2, PriorityTaskQueue

    order: List[int] = []
    queue = PriorityTaskQueue(
        lambda payload: order.append(int(payload["n"]))
    )
    try:
        for index in range(5):
            queue.submit("t{}".format(index), {"n": index}, PRIORITY_P2)
        queue.drain(timeout=15)
    finally:
        queue.stop()

    assert order == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 架构约束
# ---------------------------------------------------------------------------
def test_守护服务不依赖Web框架() -> None:
    """**只用标准库** —— 这是「瘦身」的核心承诺。"""
    source = (ROOT_DIR / "src" / "daemon" / "server.py").read_text(
        encoding="utf-8"
    )
    assert "from http.server import" in source
    for forbidden in ("import fastapi", "import uvicorn", "import jinja2"):
        assert forbidden not in source, forbidden


def test_前端资产确实不在了() -> None:
    """面板**整体**剥离。"""
    for relative in (
        "src/gui/static",
        "src/gui/templates",
        "src/gui/server.py",
        "src/gui/tray.py",
    ):
        assert not (ROOT_DIR / relative).exists(), relative


def test_依赖清单里没有Web栈() -> None:
    """``requirements.txt`` 里不该再有 FastAPI / Uvicorn / Jinja2。

    ⚠️ 只看**真正的依赖行** —— 注释里写着「v5.0.0 移除了 fastapi」是
    **说明**，不是依赖。
    """
    path = ROOT_DIR / "_构建与发布" / "requirements.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    deps = [
        line.strip().lower()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    joined = "\n".join(deps)
    for forbidden in ("fastapi", "uvicorn", "jinja2", "pystray", "httpx"):
        assert forbidden not in joined, forbidden
