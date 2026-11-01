"""v4.0.1 回归：进程树扫描的**首发卡顿**治理。

背景
====

v4.0.0 实测：Windows 上**第一次**扫进程树要 4.2 秒 ——
冷启动时枚举几百个进程、逐个打开句柄，非常贵。
用户点开雷达面板就得盯着转圈等 4 秒 —— 这是实打实的卡顿。

v4.0.1 的三招：

1. **异步预热** —— 服务启动的后台线程里立刻静默调一次
   :func:`src.proc_tree.find_bridge_hosts`，把 psutil 的句柄缓存热起来；
2. **超时熔断** —— 扫描接口最多只等 1.5 秒，超时就返回**已经扫到的
   那部分**，状态标 ``scanning``，界面提示「正在深度检索进程…」；
3. **不阻塞主线程** —— 扫描永远跑在后台线程里，请求线程只等那 1.5 秒。

本文件钉住这三件事，外加前端契约。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from src import proc_tree
from src.gui import server as gui_server
from src.gui.server import create_app
from src.proc_tree import DiscoveredAgentProcess

logger = logging.getLogger(__name__)

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)


def _fake_agent(pid: int = 777) -> DiscoveredAgentProcess:
    """造一个「已发现的 Agent 进程」。"""
    return DiscoveredAgentProcess(
        pid=pid,
        parent_pid=42,
        host_pid=42,
        depth=1,
        agent="claude",
        agent_label="Claude Code",
        exe="C:/x/claude.exe",
        cmdline="claude.exe",
        cwd="C:/x",
        confidence=0.95,
        log_path="",
        attachable=False,
        state="discovered",
        matched_by="cmdline_name",
    )


def _fake_psutil() -> Any:
    """假的 psutil 模块（只够让扫描流程跑起来）。"""
    return object()


@pytest.fixture(autouse=True)
def _reset_scan_state():
    """每个用例前把进程树扫描的**状态机**清干净。

    ⚠️ 状态与缓存都是**模块级**的（跨用例共享），
    不清的话上一个用例塞进去的假结果会被下一个用例读走。
    """
    with gui_server._cloud_proc_lock:
        gui_server._cloud_proc_state.update(
            {
                "status": "idle",
                "started_at": 0.0,
                "finished_at": 0.0,
                "items": [],
                "error": "",
            }
        )
        gui_server._cloud_proc_cache.update(
            {"ready": False, "at": 0.0, "items": []}
        )
    gui_server._cloud_proc_event.clear()
    yield
    with gui_server._cloud_proc_lock:
        gui_server._cloud_proc_state["status"] = "idle"
        gui_server._cloud_proc_state["items"] = []
        gui_server._cloud_proc_cache["ready"] = False
        gui_server._cloud_proc_cache["items"] = []
    gui_server._cloud_proc_event.clear()


@pytest.fixture()
def release_scan():
    """给「慢扫描」一个放行开关（用例结束时别把线程吊死）。"""
    gate = threading.Event()
    yield gate
    gate.set()


def _hermetic_config(tmp_path: Path) -> str:
    """造一份「只扫临时目录、什么都不排除」的配置文件。"""
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir(exist_ok=True)
    config = tmp_path / "radar.yaml"
    config.write_text(
        "radar:\n"
        "  scan_roots:\n"
        "    - {}\n"
        "  exclude_keywords: []\n".format(empty_root),
        encoding="utf-8",
    )
    return str(config)


def _make_app(tmp_path: Path):
    """造一个最小可用的看板应用。"""
    base = tmp_path / "app"
    base.mkdir(parents=True, exist_ok=True)
    watch = base / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = base / "alert.log"
    alert.write_text("", encoding="utf-8")

    def factory(info: Dict[str, Any]) -> Dict[str, Any]:
        """假工厂：只记录调用，不真的起引擎。"""
        return {
            "ok": True,
            "alert_log_path": str(base / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": info.get("cwd", ""),
            "tail_runner": None,
        }

    return create_app(
        str(watch),
        str(alert),
        start_watcher=False,
        attach_factory=factory,
        config_path=_hermetic_config(base),
    )


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
def test_默认超时就是一秒半() -> None:
    """熔断阈值必须是 1.5 秒（任务书指定）。"""
    assert gui_server.CLOUD_PROC_SCAN_TIMEOUT == 1.5


# ---------------------------------------------------------------------------
# 超时熔断
# ---------------------------------------------------------------------------
def test_扫描超时立刻返回部分结果(monkeypatch, release_scan) -> None:
    """扫描慢 → **不等它扫完**，先把手头那部分交出去。"""
    monkeypatch.setattr(gui_server, "CLOUD_PROC_SCAN_TIMEOUT", 0.2)
    entered = threading.Event()

    def slow_scan(**kwargs: Any) -> List[DiscoveredAgentProcess]:
        """假扫描：先报一个结果，再卡住。"""
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress([_fake_agent()])
        entered.set()
        release_scan.wait(timeout=10.0)
        return [_fake_agent()]

    monkeypatch.setattr(proc_tree, "scan_process_tree", slow_scan)

    started = time.monotonic()
    rows = gui_server._scan_cloud_processes()
    elapsed = time.monotonic() - started

    assert entered.wait(timeout=5.0), "假扫描没被调到"
    assert elapsed < 3.0, "超时熔断没生效，等了 {:.1f} 秒".format(elapsed)
    assert len(rows) == 1
    assert rows[0]["agent"] == "claude"

    state = gui_server._cloud_proc_scan_state()
    assert state["status"] == "scanning"
    assert state["partial"] is True


def test_扫描超时状态在后台继续(monkeypatch, release_scan) -> None:
    """超时只是**不等**，后台那一次扫描要接着跑完。"""
    monkeypatch.setattr(gui_server, "CLOUD_PROC_SCAN_TIMEOUT", 0.1)
    done = threading.Event()

    def slow_scan(**kwargs: Any) -> List[DiscoveredAgentProcess]:
        """假扫描：卡一会儿再返回。"""
        release_scan.wait(timeout=10.0)
        done.set()
        return [_fake_agent()]

    monkeypatch.setattr(proc_tree, "scan_process_tree", slow_scan)

    gui_server._scan_cloud_processes()
    release_scan.set()
    assert done.wait(timeout=5.0), "后台扫描没跑完"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        state = gui_server._cloud_proc_scan_state()
        if state["status"] == "ready":
            break
        time.sleep(0.05)
    state = gui_server._cloud_proc_scan_state()
    assert state["status"] == "ready"
    assert state["partial"] is False
    with gui_server._cloud_proc_lock:
        assert len(gui_server._cloud_proc_cache["items"]) == 1


def test_扫描跑在后台线程(monkeypatch, release_scan) -> None:
    """扫描**绝不**在请求线程里跑（否则会堵住事件循环）。"""
    monkeypatch.setattr(gui_server, "CLOUD_PROC_SCAN_TIMEOUT", 0.1)
    names: List[str] = []

    def slow_scan(**kwargs: Any) -> List[DiscoveredAgentProcess]:
        """假扫描：记下自己跑在哪个线程。"""
        names.append(threading.current_thread().name)
        release_scan.wait(timeout=10.0)
        return []

    monkeypatch.setattr(proc_tree, "scan_process_tree", slow_scan)
    gui_server._scan_cloud_processes()
    assert names, "扫描没被调到"
    assert names[0] != threading.current_thread().name
    assert names[0] == "sentinel-proc-scan"


def test_缓存命中时不重扫(monkeypatch) -> None:
    """5 秒 TTL 内不该重复扫（雷达刷新得很频繁）。"""
    calls: List[int] = []

    def counting_scan(**kwargs: Any) -> List[DiscoveredAgentProcess]:
        """假扫描：数自己被调了几次。"""
        calls.append(1)
        return [_fake_agent()]

    monkeypatch.setattr(proc_tree, "scan_process_tree", counting_scan)
    first = gui_server._scan_cloud_processes()
    second = gui_server._scan_cloud_processes()
    assert len(first) == 1
    assert len(second) == 1
    assert len(calls) == 1, "TTL 内重复扫了 {} 次".format(len(calls))


# ---------------------------------------------------------------------------
# 预热
# ---------------------------------------------------------------------------
def test_预热会调用宿主枚举(monkeypatch) -> None:
    """启动预热**必须**先调 :func:`find_bridge_hosts` 热 psutil 缓存。"""
    calls: List[str] = []
    monkeypatch.setattr(
        proc_tree,
        "find_bridge_hosts",
        lambda **kwargs: calls.append("hosts") or [],
    )
    monkeypatch.setattr(
        proc_tree,
        "scan_process_tree",
        lambda **kwargs: calls.append("scan") or [],
    )
    gui_server._warm_cloud_process_scan()
    assert calls, "预热什么都没调"
    assert calls[0] == "hosts", "预热第一步必须是 find_bridge_hosts"


def test_预热失败不影响启动(monkeypatch) -> None:
    """预热炸了只降级 —— 哨兵照样起来（红线 R5）。"""

    def boom(**kwargs: Any) -> List[Any]:
        """假装预热失败。"""
        raise RuntimeError("炸了")

    monkeypatch.setattr(proc_tree, "find_bridge_hosts", boom)
    monkeypatch.setattr(proc_tree, "scan_process_tree", boom)
    gui_server._warm_cloud_process_scan()


def test_预热期间状态是扫描中(monkeypatch) -> None:
    """预热期间状态就是「扫描中」。

    ⚠️ 这样用户此刻点开雷达面板会**等**这一轮，
    而不是另起一次全量枚举（那只会更慢、更卡）。
    """
    observed: List[str] = []

    def hosts_probe(**kwargs: Any) -> List[Any]:
        """假宿主枚举：顺手看一眼当前状态。"""
        observed.append(gui_server._cloud_proc_scan_state()["status"])
        return []

    monkeypatch.setattr(proc_tree, "find_bridge_hosts", hosts_probe)
    monkeypatch.setattr(
        proc_tree, "scan_process_tree", lambda **kwargs: []
    )
    gui_server._warm_cloud_process_scan()
    assert observed == ["scanning"]


def test_预热只触发一次完整扫描(monkeypatch) -> None:
    """预热**不能**引发两次全量扫描（那是自己跟自己抢）。"""
    calls: List[str] = []
    monkeypatch.setattr(
        proc_tree,
        "find_bridge_hosts",
        lambda **kwargs: calls.append("hosts") or [],
    )
    monkeypatch.setattr(
        proc_tree,
        "scan_process_tree",
        lambda **kwargs: calls.append("scan") or [],
    )
    gui_server._warm_cloud_process_scan()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "scan" not in calls:
        time.sleep(0.02)
    assert calls.count("hosts") == 1
    assert calls.count("scan") == 1


# ---------------------------------------------------------------------------
# 接口契约
# ---------------------------------------------------------------------------
def test_发现接口带扫描状态字段(tmp_path: Path, monkeypatch) -> None:
    """``/api/discover`` 要告诉前端「扫完了没」。"""
    monkeypatch.setattr(
        gui_server, "_scan_cloud_processes", lambda force=False: []
    )
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/discover").json()

    assert payload["ok"] is True
    assert "process_scan_status" in payload
    assert "process_scan_partial" in payload
    assert "process_scan_elapsed_ms" in payload
    assert payload["process_scan_status"] in {
        "idle",
        "scanning",
        "ready",
        "error",
    }


def test_发现接口扫描失败也带状态(tmp_path: Path, monkeypatch) -> None:
    """扫描炸了也要给状态字段（红线 R5：只降级不报错）。"""

    def boom(force: bool = False) -> List[Dict[str, Any]]:
        """假装扫描失败。"""
        raise RuntimeError("psutil 炸了")

    monkeypatch.setattr(gui_server, "_scan_cloud_processes", boom)
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/discover").json()

    assert payload["ok"] is True
    assert payload["processes"] == []
    assert "process_scan_status" in payload


# ---------------------------------------------------------------------------
# proc_tree 侧的熔断与进度
# ---------------------------------------------------------------------------
def test_进程树扫描超时收手(monkeypatch) -> None:
    """``deadline`` 到了就收手，返回已经认出的那部分。"""
    calls: List[str] = []

    def fake_hosts(deadline: Optional[float] = None) -> List[Any]:
        """假宿主枚举。"""
        calls.append("hosts")
        return []

    def fake_descendants(pid: int, max_depth: int = 6) -> List[Any]:
        """假子孙枚举。"""
        calls.append("descendants")
        return []

    monkeypatch.setattr(proc_tree, "find_bridge_hosts", fake_hosts)
    monkeypatch.setattr(
        proc_tree, "enumerate_descendants", fake_descendants
    )
    monkeypatch.setattr(proc_tree, "_psutil_module", _fake_psutil)
    proc_tree.scan_process_tree(deadline=time.monotonic() - 1.0)
    assert "descendants" not in calls, "超时后不该再去枚举子孙"


def test_进程树扫描会回报进度(monkeypatch) -> None:
    """进度回调要真的被调到（超时熔断靠它拿部分结果）。"""
    seen: List[int] = []

    def fake_hosts(deadline: Optional[float] = None) -> List[Any]:
        """假宿主枚举：给一个假的宿主。"""
        host = proc_tree.ProcessInfo(
            pid=42,
            parent_pid=1,
            name="host",
            exe="python.exe",
            cmdline="python -m webui.server",
            cwd="C:/x",
        )
        return [host]

    def fake_descendants(pid: int, max_depth: int = 6) -> List[Any]:
        """假子孙枚举：给一个能认出来的子进程。"""
        return [
            proc_tree.ProcessInfo(
                pid=43,
                parent_pid=42,
                name="claude.exe",
                exe="C:/x/claude.exe",
                cmdline="claude.exe",
                cwd="C:/x",
                depth=1,
            )
        ]

    monkeypatch.setattr(proc_tree, "find_bridge_hosts", fake_hosts)
    monkeypatch.setattr(
        proc_tree, "enumerate_descendants", fake_descendants
    )
    monkeypatch.setattr(proc_tree, "_psutil_module", _fake_psutil)
    monkeypatch.setattr(
        proc_tree, "find_log_for_process", lambda *a, **k: ""
    )
    monkeypatch.setattr(proc_tree, "_pid_alive", lambda pid: True)

    proc_tree.scan_process_tree(
        on_progress=lambda rows: seen.append(len(rows))
    )
    assert seen, "进度回调一次都没被调"
    assert seen[-1] >= 1


# ---------------------------------------------------------------------------
# 前端契约
# ---------------------------------------------------------------------------
def test_前端有深度检索提示() -> None:
    """界面要如实显示「正在深度检索进程…」。"""
    source = Path(STATIC_DIR, "dashboard.js").read_text(encoding="utf-8")
    assert "正在深度检索进程…" in source
    assert "process_scan_status" in source


def test_前端有轮询与清理() -> None:
    """扫完就停、关窗就停 —— 不能一直打接口。"""
    source = Path(STATIC_DIR, "dashboard.js").read_text(encoding="utf-8")
    assert "pollProcessScan" in source
    assert "clearProcessPoll" in source
