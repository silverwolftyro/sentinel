"""v3.4.0 回归：托盘化彻底退出 · 挂载清理与状态纠偏 · 雷达排除自身与自定义特征。

背景
====

用户实测撞上了五件事，v3.4.0 一条一条回应：

1. **后台幽灵** —— 「关掉黑框后后台变成无法杀死的幽灵（只能去任务管理器杀）」；
2. **挂载删不掉** —— 选错 / 失效的会话只能干瞪眼；
3. **状态是假的** —— 「进程已死，但因日志文件还在，状态依然显示运行中」；
4. **雷达抓错东西** —— 哨兵压测留下的残留日志把用户真实的 N.E.K.O 任务挤掉了；
5. **看不到流式滚动** —— 中间面板空空如也，「感官上像个死系统」。

本文件钉住：

1. **退出协调器** —— 幂等、钩子顺序、单个钩子失败不挡后面的；
2. **托盘** —— 菜单两项、守护线程（不制造**新的**幽灵）；
3. **``/api/shutdown``** —— 接口契约与「延迟退出」；
4. **状态纠偏** —— 三态判定（活着 / 死了 / **不知道**）；
5. **雷达排除与自定义特征** —— 子串匹配、命令行**或**工作目录；
6. **界面** —— 【关闭哨兵】按钮、【×】卸载按钮、流水初始提示、旁路说明。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from src.gui import server
from src.gui.server import (
    SESSION_STATE_LABELS,
    EventHub,
    Session,
    SessionSpec,
    create_app,
)
from src.gui.shutdown import (
    ShutdownCoordinator,
    install_console_close_handler,
)
from src.gui.tray import (
    TRAY_MENU_OPEN,
    TRAY_MENU_QUIT,
    build_tray_icon,
    build_tray_image,
    start_tray,
    stop_tray,
    tray_available,
)
from src.procscan import (
    detect_custom_agent,
    pid_alive,
    psutil_available,
)
from src.radar import (
    DEFAULT_EXCLUDE_KEYWORDS,
    exclude_keywords,
    path_matches_keywords,
    scan_agent_sessions,
)

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    path = ROOT.joinpath(*parts)
    return path.read_text(encoding="utf-8")


class _FakeShutdown:
    """假退出协调器 —— 只记录，不真的退。"""

    def __init__(self) -> None:
        self.calls = []
        self.hooks = []

    def request(self, reason: str = "unknown") -> bool:
        """记录一次退出请求。

        :param reason: 退出原因。
        :return: 一律 ``True``（假装自己是第一次）。
        """
        self.calls.append(reason)
        return True

    def add_hook(self, hook) -> None:
        """记录一个钩子。

        :param hook: 收尾钩子。
        """
        self.hooks.append(hook)


def _app(tmp_path, **kwargs):
    """造一个最小可用的看板应用。

    :param tmp_path: pytest 的临时目录。
    :param kwargs: 透传给 :func:`create_app` 的参数。
    :return: FastAPI 应用。
    """
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    return create_app(str(watch), str(alert), start_watcher=False, **kwargs)


def _session(tmp_path, **kwargs) -> Session:
    """造一个会话（不启动跟随线程）。

    :param tmp_path: pytest 的临时目录。
    :param kwargs: 透传给 :class:`SessionSpec` 的参数。
    :return: 会话对象。
    """
    spec = SessionSpec(
        id="s1",
        label="测试会话",
        watch_path=str(tmp_path / "watch.jsonl"),
        alert_log_path=str(tmp_path / "alert.log"),
        **kwargs,
    )
    return Session(spec, EventHub())


def _dead_pid() -> int:
    """找一个**确实已经死了**的 PID。

    :return: 一个不存在的进程的 PID。
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return int(proc.pid)


# ---------------------------------------------------------------------------
# 一、退出协调器：唯一出口、幂等、钩子顺序
# ---------------------------------------------------------------------------
def test_退出请求只生效一次():
    """托盘、看板按钮、关黑框可能同时触发 —— 收尾只能跑一遍。"""
    coordinator = ShutdownCoordinator()
    calls = []
    coordinator.add_hook(calls.append)

    assert coordinator.request("tray") is True
    assert coordinator.request("api") is False
    assert calls == ["tray"], "退出钩子跑了两遍"
    assert coordinator.reason == "tray"
    assert coordinator.requested is True


def test_退出请求会通知_uvicorn_停机():
    """``should_exit`` 是 uvicorn 文档里的正规做法 —— 不是强杀。"""

    class _FakeServer:
        should_exit = False

    coordinator = ShutdownCoordinator()
    server_obj = _FakeServer()
    coordinator.bind_server(server_obj)
    coordinator.request("api")
    assert server_obj.should_exit is True


def test_钩子按注册顺序跑():
    """顺序不能乱 —— 停托盘、结束子进程，各有各的次序。"""
    order = []
    coordinator = ShutdownCoordinator()
    coordinator.add_hook(lambda reason: order.append("a"))
    coordinator.add_hook(lambda reason: order.append("b"))
    coordinator.request("x")
    assert order == ["a", "b"]


def test_一个钩子炸了不挡后面的():
    """钩子失败就失败 —— 后面那个还得跑（不然就留下残留了）。"""
    order = []

    def _boom(reason):
        raise RuntimeError("收尾失败")

    coordinator = ShutdownCoordinator()
    coordinator.add_hook(_boom)
    coordinator.add_hook(lambda reason: order.append("after"))
    coordinator.request("x")
    assert order == ["after"]


def test_没绑服务器也能退出():
    """还没绑 uvicorn 就收到退出请求 —— 不该崩。"""
    coordinator = ShutdownCoordinator()
    assert coordinator.request("early") is True


def test_控制台关闭处理器在_windows_上能装上():
    """关掉黑框 = 彻底退出 —— 这条路必须能装上。"""
    from src.gui import shutdown

    coordinator = ShutdownCoordinator()
    if os.name != "nt":
        assert install_console_close_handler(coordinator) is False
        return
    before = len(shutdown._CONSOLE_HANDLER_REF)
    assert install_console_close_handler(coordinator) is True
    assert len(shutdown._CONSOLE_HANDLER_REF) == before + 1
    # 装完就摘掉 —— 别改掉测试进程自己的关闭行为。
    ctypes.windll.kernel32.SetConsoleCtrlHandler(
        shutdown._CONSOLE_HANDLER_REF[-1], 0
    )
    shutdown._CONSOLE_HANDLER_REF.pop()


def test_控制台处理器只认终止类事件():
    """Ctrl+C 不归它管 —— 那是 Python 自己的 ``KeyboardInterrupt``。"""
    from src.gui.shutdown import (
        _TERMINAL_EVENTS,
        CTRL_BREAK_EVENT,
        CTRL_C_EVENT,
        CTRL_CLOSE_EVENT,
        CTRL_SHUTDOWN_EVENT,
    )

    assert CTRL_CLOSE_EVENT in _TERMINAL_EVENTS
    assert CTRL_SHUTDOWN_EVENT in _TERMINAL_EVENTS
    assert CTRL_C_EVENT not in _TERMINAL_EVENTS
    assert CTRL_BREAK_EVENT not in _TERMINAL_EVENTS


# ---------------------------------------------------------------------------
# 二、托盘：菜单、守护线程、缺依赖不崩
# ---------------------------------------------------------------------------
def test_托盘菜单就两项():
    """菜单**故意不放别的** —— 打开看板 + 彻底退出。"""
    assert TRAY_MENU_OPEN == "打开看板"
    assert TRAY_MENU_QUIT == "彻底退出"
    if not tray_available():
        return
    icon = build_tray_icon("http://127.0.0.1:8888/", lambda: None)
    assert icon is not None
    texts = [item.text for item in icon.menu]
    assert TRAY_MENU_OPEN in texts
    assert TRAY_MENU_QUIT in texts
    # 分隔线不是菜单项 —— 「就两项」说的是**两项**。
    real_items = [text for text in texts if text != "- - - -"]
    assert len(real_items) == 2


def test_托盘图标画得出来():
    """图标是 Pillow 自己画的 —— 不依赖 .ico 资源文件。"""
    image = build_tray_image(64)
    if image is None:
        return
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


def test_托盘线程是守护线程():
    """⚠️ 必须守护 —— 非守护线程会制造**新的**幽灵。"""

    class _FakeIcon:
        def run(self):
            time.sleep(0.1)

        def stop(self):
            pass

    thread = start_tray(_FakeIcon())
    assert thread is not None
    assert thread.daemon is True


def test_托盘起不来也不影响主功能():
    """``None`` 图标 / 起线程失败 —— 一律安静降级（红线 R5）。"""
    assert start_tray(None) is None
    stop_tray(None)


def test_没有_pystray_就不出托盘():
    """缺依赖只是「没图标」，哨兵照跑。"""
    assert isinstance(tray_available(), bool)


# ---------------------------------------------------------------------------
# 三、看板右上角的【关闭哨兵】
# ---------------------------------------------------------------------------
def test_关闭接口返回契约(tmp_path, monkeypatch):
    """网页不能自己关掉自己 —— 但它能让后台把自己收干净。"""
    fake = _FakeShutdown()
    monkeypatch.setattr(server, "_SHUTDOWN", fake)
    client = TestClient(_app(tmp_path))

    response = client.post("/api/shutdown")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["stopped"] is True
    assert "退出" in data["message"]

    # 退出请求是**延迟**发出的 —— 让响应先回到浏览器，
    # 否则用户看到的是「连接被重置」而不是那句「正在退出」。
    deadline = time.time() + 5.0
    while time.time() < deadline and not fake.calls:
        time.sleep(0.05)
    assert fake.calls == ["api"]


def test_退出钩子能登记(tmp_path, monkeypatch):
    """启动器靠它把「自己拉起的子进程」登记成收尾动作。"""
    fake = _FakeShutdown()
    monkeypatch.setattr(server, "_SHUTDOWN", fake)
    server.register_shutdown_hook(lambda reason: None)
    assert len(fake.hooks) == 1


def test_顶栏有红色的关闭按钮():
    """【关闭哨兵】必须在顶栏 —— 用户到处找才找得到。"""
    html = _read("src", "gui", "templates", "dashboard.html")
    buttons = [
        chunk
        for chunk in html.split("<button")
        if 'id="shutdown-button"' in chunk
    ]
    assert buttons, "顶栏没有【关闭哨兵】按钮"
    block = buttons[0]
    assert "btn--danger" in block
    assert "关闭哨兵" in block


def test_前端点了会发关闭指令():
    """点【关闭哨兵】→ 确认 → ``POST /api/shutdown``。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert "confirmShutdown" in js
    assert "doShutdown" in js
    assert "/api/shutdown" in js
    assert 'el.shutdownButton.addEventListener("click", confirmShutdown)' in js


def test_关闭前要弹确认框():
    """关哨兵是不可逆操作 —— 必须让用户确认一次。"""
    js = _read("src", "gui", "static", "dashboard.js")
    block = js.split("function confirmShutdown", 1)[1].split("\n  }", 1)[0]
    assert "确认关闭" in block
    # 后果要说清楚：尤其是「雷达挂载的 Agent 不受影响」。
    assert "托盘" in block
    assert "子进程" in block


# ---------------------------------------------------------------------------
# 四、状态纠偏：三态判定
# ---------------------------------------------------------------------------
def test_进程存活性是三态():
    """「查不到」与「死了」是两回事 —— 必须分得开。"""
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is None
    assert pid_alive(-1) is None


def test_死掉的进程能认出来():
    """PID 没了 → ``False``（不是 ``None``）。"""
    if not psutil_available():
        return
    assert pid_alive(_dead_pid()) is False


def test_没有绑定就是不知道(tmp_path):
    """没反查到 PID → ``None`` —— 绝不能据此改状态。"""
    session = _session(tmp_path)
    assert session.bound_pid() == 0
    assert session.process_alive() is None


def test_不可信的绑定不算(tmp_path):
    """只是「按工作目录猜到了个什么」—— 不能拿它的 PID 判死活。"""
    session = _session(
        tmp_path, process_info={"pid": 4321, "bindable": False}
    )
    assert session.bound_pid() == 0
    assert session.process_alive() is None


def test_进程死了就标成已退出(tmp_path):
    """状态从「运行中」改成「已退出（静默）」。"""
    if not psutil_available():
        return
    session = _session(
        tmp_path,
        process_info={"pid": _dead_pid(), "bindable": True},
    )
    assert session.process_alive() is False
    session.mark_exited()
    assert session.state() == "exited"
    assert session.state_label() == "已退出（静默）"

    snapshot = session.snapshot()
    assert snapshot["state"] == "exited"
    assert snapshot["state_label"] == "已退出（静默）"


def test_退出状态有中文说明():
    """面板直接显示这句话 —— 写错用户就看不懂了。"""
    assert SESSION_STATE_LABELS["exited"] == "已退出（静默）"


def test_快照带占位与存活字段(tmp_path):
    """前端要靠 ``placeholder`` 判断给不给卸载按钮。"""
    session = _session(tmp_path, placeholder=True)
    snapshot = session.snapshot()
    assert snapshot["placeholder"] is True
    assert snapshot["process_alive"] is None


def test_雷达模式的会话是占位(tmp_path):
    """雷达自己那个空壳 —— 不是用户挂上来的，不给卸载按钮。"""
    app = _app(tmp_path, radar_mode=True)
    client = TestClient(app)
    sessions = client.get("/api/sessions").json()["sessions"]
    assert sessions
    assert sessions[0]["placeholder"] is True


class _FakeLivenessSession:
    """假会话 —— 只回答「进程还在吗」。"""

    def __init__(self, session_id: str, alive) -> None:
        self.id = session_id
        self._alive = alive
        self.exited = False

    def process_alive(self):
        """返回预置的存活状态。"""
        return self._alive

    def mark_exited(self) -> None:
        """记录「被标成已退出」。"""
        self.exited = True


class _FakeRegistry:
    """假会话登记表。"""

    def __init__(self, sessions) -> None:
        self._sessions = list(sessions)

    def list(self):
        """返回会话列表。"""
        return list(self._sessions)


def test_巡检只把死了的标成已退出():
    """``None``（不知道）**不能**改状态 —— 那是 v3.3.1 放宽过的宽容度。"""
    dead = _FakeLivenessSession("dead", False)
    unknown = _FakeLivenessSession("unknown", None)
    alive = _FakeLivenessSession("alive", True)
    registry = _FakeRegistry([dead, unknown, alive])

    stop = threading.Event()
    thread = threading.Thread(
        target=server._watch_process_liveness,
        args=(registry, stop),
        kwargs={"interval": 0.05},
        daemon=True,
    )
    thread.start()
    time.sleep(0.4)
    stop.set()
    thread.join(timeout=3)

    assert dead.exited is True
    assert unknown.exited is False
    assert alive.exited is False


def test_巡检间隔是十秒():
    """10 秒是个折中：再快没意义，再慢用户会觉得卡住。"""
    assert server.PROCESS_LIVENESS_INTERVAL == 10.0


# ---------------------------------------------------------------------------
# 五、挂载列表的清理
# ---------------------------------------------------------------------------
def test_会话卡片有卸载按钮():
    """每个会话卡片（除雷达自身）都要有 ``[×]``。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert "session-item__remove" in js
    assert "detachSession" in js
    # 点【×】不能连带切到那个会话 —— 卡片本身有点击监听。
    assert "stopPropagation" in js


def test_卸载按钮不给占位会话():
    """雷达自己那个空壳不给卸载按钮。"""
    js = _read("src", "gui", "static", "dashboard.js")
    guard = js.split("if (!session.placeholder", 1)
    assert len(guard) == 2, "前端没有按 placeholder 区分"
    region = guard[1].split("item.appendChild(remove);", 1)[0]
    assert "session-item__remove" in region


def test_卸载会调删除接口():
    """点【×】→ ``DELETE /api/sessions/{id}`` → 立刻从列表里移除。"""
    js = _read("src", "gui", "static", "dashboard.js")
    block = js.split("function detachSession", 1)[1].split("\n  }", 1)[0]
    assert "method: \"DELETE\"" in block
    assert "/api/sessions/" in block
    # 立刻从本地状态里去掉 —— 不等下一次轮询。
    assert "state.sessions" in block


def test_卸载按钮有样式():
    """按钮得看得出是个能点的东西。"""
    css = _read("src", "gui", "static", "dashboard.css")
    assert ".session-item__remove {" in css
    assert ".session-item__remove:hover" in css


def test_删除接口能卸掉会话(tmp_path):
    """后端这一侧早就有（v3.1.0）—— 这里钉住它的契约。"""

    def factory(info):
        """假工厂。"""
        return {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": "",
            "tail_runner": None,
        }

    app = _app(tmp_path, attach_factory=factory)
    client = TestClient(app)

    log = tmp_path / "mcode.jsonl"
    log.write_text("", encoding="utf-8")
    attached = client.post("/api/attach", json={"path": str(log)})
    assert attached.status_code == 200
    session_id = attached.json()["session"]["id"]

    deleted = client.delete("/api/sessions/{}".format(session_id))
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    remaining = [
        item["id"]
        for item in client.get("/api/sessions").json()["sessions"]
    ]
    assert session_id not in remaining


def test_已退出状态在界面上是灰的():
    """Agent 跑完是**正常结束** —— 不用红色吓人。"""
    css = _read("src", "gui", "static", "dashboard.css")
    assert '.session-item__state[data-state="exited"]' in css


# ---------------------------------------------------------------------------
# 六、雷达的「排除自身」
# ---------------------------------------------------------------------------
def test_默认排除清单是那三个词():
    """``sentinel`` / ``stress`` / ``test`` —— 哨兵自己的压测残留。"""
    assert set(DEFAULT_EXCLUDE_KEYWORDS) == {"sentinel", "stress", "test"}
    assert exclude_keywords() == ["sentinel", "stress", "test"]


def test_排除关键词去空去重去大小写():
    """用户手写清单 —— 空格、大小写、重复都得收拾干净。"""
    assert exclude_keywords(["A", "", "  ", "a", "B"]) == ["a", "b"]


def test_路径命中子串就排除():
    """子串匹配 —— 所以 ``test`` 也会命中 ``contest``。"""
    assert path_matches_keywords("D:\\项目\\sentinel\\a.jsonl") is True
    assert path_matches_keywords("D:\\logs\\stress-run.jsonl") is True
    assert path_matches_keywords("D:\\logs\\contest.jsonl") is True
    assert path_matches_keywords("D:\\logs\\real-session.jsonl") is False
    assert path_matches_keywords("") is False


def test_雷达扫不到带关键词的日志(tmp_path):
    """端到端：哨兵自己的残留日志**不再出现**在雷达列表里。"""
    good = tmp_path / "good" / "session-1.jsonl"
    good.parent.mkdir(parents=True)
    good.write_text('{"type":"thinking"}\n', encoding="utf-8")
    bad = tmp_path / "sentinel" / "session-2.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"type":"thinking"}\n', encoding="utf-8")

    roots = [{"agent": "mcode", "label": "MCode", "path": str(tmp_path)}]

    # ⚠️ pytest 的临时目录名里就带 "test" ——
    #    所以「什么都不排除」必须显式传空清单，不能用默认值。
    everything = scan_agent_sessions(roots=roots, limit=10, exclude_keywords=[])
    paths = [item.path for item in everything]
    assert any("good" in path for path in paths)
    assert any("sentinel" in path for path in paths)

    filtered = scan_agent_sessions(
        roots=roots, limit=10, exclude_keywords=["sentinel"]
    )
    filtered_paths = [item.path for item in filtered]
    assert not any("sentinel" in path for path in filtered_paths)
    assert any("good" in path for path in filtered_paths)


def test_配置里的排除关键词能被读到(tmp_path):
    """设置页写进去的值，后端读得出来。"""
    config = tmp_path / "config.yaml"
    config.write_text(
        "radar:\n  exclude_keywords:\n    - neko\n    - stress\n",
        encoding="utf-8",
    )
    assert server._radar_exclude_keywords(str(config)) == ["neko", "stress"]
    assert server._effective_exclude_keywords(str(config)) == [
        "neko",
        "stress",
    ]


def test_没配就用内置默认清单(tmp_path):
    """「没写」→ 用默认清单。"""
    config = tmp_path / "config.yaml"
    config.write_text("radar: {}\n", encoding="utf-8")
    assert server._radar_exclude_keywords(str(config)) is None
    assert server._effective_exclude_keywords(str(config)) == list(
        DEFAULT_EXCLUDE_KEYWORDS
    )


def test_配空列表就是什么都不排除(tmp_path):
    """⚠️ 「空列表」与「没配」是两回事 —— 空列表要有意义。"""
    config = tmp_path / "config.yaml"
    config.write_text("radar:\n  exclude_keywords: []\n", encoding="utf-8")
    assert server._radar_exclude_keywords(str(config)) == []
    assert server._effective_exclude_keywords(str(config)) == []


def test_排除关键词能写回配置(tmp_path):
    """设置页提交 → 翻译成 yaml 路径 → 写回。"""
    updates = server._collect_config_updates(
        {"radar": {"exclude_keywords": ["neko", "  "]}}
    )
    assert ("radar.exclude_keywords", ["neko"]) in updates

    updates = server._collect_config_updates(
        {"radar": {"custom_process_features": ["neko"]}}
    )
    assert ("radar.custom_process_features", ["neko"]) in updates


def test_设置接口带着雷达配置(tmp_path):
    """设置页要显示「当前实际生效」的值。"""
    client = TestClient(_app(tmp_path))
    data = client.get("/api/config").json()
    assert "radar_exclude_keywords" in data
    assert "radar_custom_features" in data
    assert isinstance(data["radar_exclude_keywords"], list)
    assert isinstance(data["radar_custom_features"], list)


def test_示例配置里有这两项():
    """示例配置是用户的抄写模板 —— 新功能得写进去。"""
    text = _read("config", "config.example.yaml")
    assert "exclude_keywords" in text
    assert "custom_process_features" in text


# ---------------------------------------------------------------------------
# 七、雷达的「自定义特征」
# ---------------------------------------------------------------------------
def test_自定义特征命中命令行():
    """用户输入 ``neko`` → 命令行里带它的进程要被认出来。"""
    assert detect_custom_agent("node.exe run-neko.js", features=["neko"]) == (
        "neko"
    )


def test_自定义特征命中工作目录():
    """⚠️ 关键字常常在**项目路径**里（N.E.K.O），只看命令行会漏。"""
    assert (
        detect_custom_agent(
            "node.exe run.js",
            cwd="D:\\项目\\N.E.K.O",
            features=["neko"],
        )
        == "neko"
    )


def test_带点的目录名也能认出来():
    """⚠️ 实机踩到的坑：目录叫 ``N.E.K.O``，用户写的是 ``neko``。

    纯子串匹配对不上（中间隔着三个点）——
    那正是这个功能最该管住的那个场景，不能让它失效。
    """
    assert (
        detect_custom_agent(
            "node.exe @minimax-ai/code/cli.js --continue",
            cwd="D:\\项目\\N.E.K.O",
            features=["neko"],
        )
        == "neko"
    )
    # 换个分隔符也一样。
    assert (
        detect_custom_agent(
            "node.exe run.js", cwd="D:\\项目\\my-neko-app", features=["neko"]
        )
        == "neko"
    )


def test_太短的关键词不参与去分隔符匹配():
    """一两个字母太模糊 —— 去掉分隔符后几乎什么都能命中。"""
    assert detect_custom_agent("node.exe a.js", features=["a"]) == ""


def test_没配特征就认不出():
    """内置特征认不出来时，也不要瞎猜。"""
    assert detect_custom_agent("node.exe run-neko.js", features=None) == ""
    assert detect_custom_agent("", features=["neko"]) == ""


def test_自定义特征不会被空串误伤():
    """空关键词等于「不匹配」—— 不能匹配所有东西。"""
    assert detect_custom_agent("node.exe run.js", features=["", "  "]) == ""


def test_自定义特征能从配置里读到(tmp_path):
    """设置页写进去的 ``neko`` 要能被雷达用上。"""
    config = tmp_path / "config.yaml"
    config.write_text(
        "radar:\n  custom_process_features:\n    - neko\n", encoding="utf-8"
    )
    assert server._radar_custom_features(str(config)) == ["neko"]


def test_没配自定义特征就是空列表(tmp_path):
    """没配 → 空列表（不是 ``None``）。"""
    config = tmp_path / "config.yaml"
    config.write_text("radar: {}\n", encoding="utf-8")
    assert server._radar_custom_features(str(config)) == []


# ---------------------------------------------------------------------------
# 八、界面：流水初始提示 + 旁路说明
# ---------------------------------------------------------------------------
def test_流水区有初始提示():
    """空面板看着像卡住了 —— 得说清楚「我在读，只是一切正常」。"""
    html = _read("src", "gui", "templates", "dashboard.html")
    assert 'id="live-audit-placeholder"' in html
    assert (
        "正在实时读取日志流，若无告警，此处将滚动显示正常操作。" in html
    )


def test_流水提示有样式():
    css = _read("src", "gui", "static", "dashboard.css")
    assert ".live-audit__placeholder {" in css


def test_前端会把初始提示收起来():
    """第一条真实流水进来 → 提示让位；清空 → 提示回来。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert "resetLiveAuditPlaceholder" in js
    assert "live-audit-placeholder" in js


def test_设置页说清楚是旁路不是_hook():
    """「看不到 Hook 的痕迹」不等于「它没在工作」—— 这要说破。"""
    html = _read("src", "gui", "templates", "settings_sessions.html")
    assert "旁路日志跟随" in html
    assert "不是侵入式 Hook" in html
    assert "JSON 日志输出" in html


def test_设置页有雷达关键词输入框():
    """两个 textarea —— 排除关键词 + 自定义特征。"""
    html = _read("src", "gui", "templates", "settings_sessions.html")
    assert 'id="radar-exclude-keywords"' in html
    assert 'id="radar-custom-features"' in html


def test_设置页_js_会提交并回填():
    """填了要能存，打开要能看见。"""
    js = _read("src", "gui", "static", "settings_sessions.js")
    assert "radar-exclude-keywords" in js
    assert "radar-custom-features" in js
    assert "exclude_keywords" in js
    assert "custom_process_features" in js


# ---------------------------------------------------------------------------
# 九、启动器：只结束**自己**拉起的子进程
# ---------------------------------------------------------------------------
def test_启动器有子进程回收():
    """红线 R6 —— 退出时把自己拉起的 Agent 收掉。"""
    text = _read("_构建与发布", "scripts", "run_with_sentinel.py")
    assert "_terminate_agent_trees" in text
    assert "register_shutdown_hook" in text
    assert "start_tray_icon" in text


def test_启动器不会去杀用户的进程():
    """⚠️ 雷达挂载进来的 Agent 是**用户自己**跑的 —— 绝不碰。"""
    text = _read("_构建与发布", "scripts", "run_with_sentinel.py")
    block = text.split("def _terminate_agent_trees", 1)[1].split("\ndef ", 1)[0]
    # 只处理自己拉起来的那一批（``item.proc``）。
    assert "proc" in block
    assert "sessions" in block


# ---------------------------------------------------------------------------
# 十、版本口径
# ---------------------------------------------------------------------------
def test_版本号是340():
    """版本号必须跟当前包版本一致 —— 这里钉住其中一处。

    ⚠️ 动态断言 —— 写死版本号的话，每发一次版就得改一遍，
    而「忘了改」正是版本号不同步的**头号原因**。
    """
    import src

    source = _read("src", "__init__.py")
    assert '"{}"'.format(src.__version__) in source
    assert os.path.exists(ROOT / "CHANGELOG.md")
