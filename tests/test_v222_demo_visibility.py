"""v2.2.2 回归：演示模式**必须显式说破**。

背景
====

v2.2.1 之前，用户双击 ``sentinel.exe`` 进入演示模式，
看到一屏告警卡片（包括「删除文件」这种**假红线**），
却完全有理由以为：**哨兵监控到了我正在独立运行的那个 Agent**。

界面没说谎，但界面**没说全** —— 这就叫认知欺骗。
本文件把「演示模式必须显式暴露」钉成回归，防止以后被改回去。

覆盖三块：

1. **终端说明** —— 6 行、逐字固定、GBK 编码下也不炸；
2. **看板横幅** —— 演示模式有红色横幅，真实模式**一个都没有**；
3. **会话名** —— 演示模式叫「[演示] 默认会话」。
"""

from __future__ import annotations

import io
import re
import sys
import types

from src.console import (
    DEMO_NOTICE_LINES,
    DEMO_NOTICE_WIDTH,
    print_demo_notice,
)
from src.gui.server import create_app

# ---------------------------------------------------------------------------
# 任务要求的**逐字**文案 —— 这是一份「诚实性契约」
# ---------------------------------------------------------------------------
EXPECTED_NOTICE = (
    "==================================================",
    " 注意：您当前进入的是【演示模式】！",
    " 我们正在使用模拟数据为您展示功能。",
    " 若要监控真实运行的 Agent，请使用以下命令：",
    ' 启动哨兵.bat --agent mcode --cwd "你的项目路径" "任务指令"',
    "==================================================",
)

#: 看板红色横幅的**逐字**文案。
EXPECTED_BANNER_TEXT = (
    "⚠ 当前为演示模式，展示的是模拟数据，未监控任何真实进程！"
)


class _Recorder:
    """记录打印内容的替身。"""

    def __init__(self) -> None:
        self.buffer = io.StringIO()

    def write(self, text: str) -> int:
        """写进缓冲区。"""
        return self.buffer.write(text)

    def flush(self) -> None:
        """没有缓冲区要冲。"""

    @property
    def text(self) -> str:
        """已写出的内容。"""
        return self.buffer.getvalue()


class _GbkStream:
    """模拟「stdout 被重定向到 GBK 文件」的场景。

    Windows 上 stdout 一旦被重定向，Python 会退回系统 ANSI 代码页，
    编不出来的字符会直接抛 ``UnicodeEncodeError`` ——
    而这段提示恰好在**启动流程**里，炸了就等于启动失败。
    """

    encoding = "gbk"

    def __init__(self) -> None:
        self.buffer = io.StringIO()

    def write(self, text: str) -> int:
        """按 GBK 编码写 —— 编不出来就抛。"""
        data = text.encode(self.encoding)
        self.buffer.write(data.decode(self.encoding))
        return len(text)

    def flush(self) -> None:
        """没有缓冲区要冲。"""

    @property
    def text(self) -> str:
        """已写出的内容。"""
        return self.buffer.getvalue()


# ---------------------------------------------------------------------------
# 任务二：终端输出的显性化
# ---------------------------------------------------------------------------
def test_终端说明逐字固定():
    """这 6 行是产品承诺的文案 —— 不能被随手改写。"""
    assert tuple(DEMO_NOTICE_LINES) == EXPECTED_NOTICE


def test_终端说明正好六行():
    """要求就是 6 行。"""
    assert len(DEMO_NOTICE_LINES) == 6


def test_分隔线宽度固定():
    """两条分隔线等宽（50 个 ``=``）。"""
    assert DEMO_NOTICE_WIDTH == 50
    assert DEMO_NOTICE_LINES[0] == "=" * 50
    assert DEMO_NOTICE_LINES[-1] == "=" * 50


def test_说明了怎么监控真实Agent():
    """光说「这是演示」不够 —— 还得告诉用户**怎么**用真的。"""
    text = "\n".join(DEMO_NOTICE_LINES)
    assert "启动哨兵.bat --agent mcode" in text
    assert "未监控任何真实进程" not in text  # 这是看板横幅的文案


def test_打印演示说明():
    """打印出来的内容要与约定一字不差。"""
    recorder = _Recorder()
    print_demo_notice(stream=recorder)
    assert recorder.text == "\n".join(EXPECTED_NOTICE) + "\n"


def test_打印演示说明在GBK下不炸():
    """编码兜底：宁可换成 ``?``，也不能让提示把启动带崩。"""
    stream = _GbkStream()
    print_demo_notice(stream=stream)
    assert "演示模式" in stream.text


def test_打印演示说明不抛异常():
    """再离谱的流也不能炸 —— 这段话在启动路径上。"""

    class _Broken:
        """什么都不支持。"""

        encoding = "utf-8"

        def write(self, text: str) -> int:
            """模拟流已经关闭。"""
            raise OSError("流已关闭")

        def flush(self) -> None:
            """没有缓冲区要冲。"""

    # 流坏了就说明打不出来 —— 但**不能**把异常甩到调用方。
    print_demo_notice(stream=_Broken())


# ---------------------------------------------------------------------------
# 任务一：看板的醒目标识
# ---------------------------------------------------------------------------
def _make_app(tmp_path, **kwargs):
    """造一个最小可用的看板应用。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    return create_app(
        str(watch), str(alert), start_watcher=False, **kwargs
    )


def _html(app):
    """取首页 HTML。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        return client.get("/").text


def test_演示模式有红色横幅(tmp_path):
    """演示模式必须有**一整条**红色横幅。"""
    html = _html(_make_app(tmp_path, demo_mode=True))
    assert "demo-banner" in html
    assert EXPECTED_BANNER_TEXT in html


def test_横幅文案逐字固定(tmp_path):
    """文案是逐字定死的 —— 少一个字都算不合格。"""
    html = _html(_make_app(tmp_path, demo_mode=True))
    assert "⚠ 当前为演示模式，展示的是模拟数据，未监控任何真实进程！" in html


def test_真实模式没有横幅(tmp_path):
    """真实监控时**绝不能**出现演示横幅 —— 否则就是反过来骗人。"""
    html = _html(_make_app(tmp_path))
    assert "demo-banner" not in html
    assert EXPECTED_BANNER_TEXT not in html


def test_演示模式有斜条纹底纹(tmp_path):
    """背景红色斜条纹 —— 铺满全屏，跑不掉。"""
    html = _html(_make_app(tmp_path, demo_mode=True))
    assert "is-demo" in html
    # ⚠️ v3.9.0：body 的 class 多了 ``is-dashboard`` ——
    #    它是移动端 Tab 显隐规则的**作用域限定**
    #    （设置页也用 .topbar__actions，不限定会误伤设置页）。
    #    所以不能再断言整串相等，只断言「is-demo 在 body 的 class 里」。
    body = re.search(r"<body[^>]*>", html)
    assert body
    assert "is-demo" in body.group(0)


def test_真实模式没有斜条纹(tmp_path):
    """真实模式不挂条纹。"""
    html = _html(_make_app(tmp_path))
    assert "is-demo" not in html


def test_演示模式有DEMO徽章(tmp_path):
    """顶栏除了横幅，还要有一枚 DEMO MODE 徽章。"""
    html = _html(_make_app(tmp_path, demo_mode=True))
    assert "DEMO MODE" in html


def test_真实模式没有DEMO徽章(tmp_path):
    """真实模式不挂演示徽章。"""
    html = _html(_make_app(tmp_path))
    assert "DEMO MODE" not in html


def test_告警面板标注模拟数据(tmp_path):
    """假告警旁边必须贴着「模拟数据」标签。"""
    html = _html(_make_app(tmp_path, demo_mode=True))
    assert html.count("模拟数据") >= 2


# ---------------------------------------------------------------------------
# 任务一之三：会话名
# ---------------------------------------------------------------------------
def test_演示模式会话名带前缀(tmp_path):
    """左侧会话列表里必须写明「[演示]」。"""
    app = _make_app(tmp_path, demo_mode=True)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        payload = client.get("/api/sessions").json()
    assert payload["sessions"][0]["label"] == "[演示] 默认会话"


def test_真实模式会话名不变(tmp_path):
    """真实监控时不该被「[演示]」污染。"""
    app = _make_app(tmp_path)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        payload = client.get("/api/sessions").json()
    assert payload["sessions"][0]["label"] == "默认会话"


def test_显式会话也加前缀(tmp_path):
    """多会话模式下同样要加前缀（而且不能改坏调用方的对象）。"""
    from src.gui.server import SessionSpec
    from src.gui.server import create_app as _create_app

    spec = SessionSpec(id="s1", label="我的会话")
    app = _create_app(sessions=[spec], start_watcher=False, demo_mode=True)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        payload = client.get("/api/sessions").json()
    assert payload["sessions"][0]["label"] == "[演示] 我的会话"
    # 调用方传进来的对象不能被就地改坏。
    assert spec.label == "我的会话"


def test_前缀不会重复叠加(tmp_path):
    """已经有前缀就别再加一遍。"""
    from src.gui.server import SessionSpec
    from src.gui.server import create_app as _create_app

    spec = SessionSpec(id="s1", label="[演示] 默认会话")
    app = _create_app(sessions=[spec], start_watcher=False, demo_mode=True)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        payload = client.get("/api/sessions").json()
    assert payload["sessions"][0]["label"] == "[演示] 默认会话"


# ---------------------------------------------------------------------------
# 任务二之接线：大字报**之后**紧接打印说明
# ---------------------------------------------------------------------------
def _fake_uvicorn() -> types.ModuleType:
    """造一个**假的** uvicorn 模块。

    ⚠️ v3.0.1 起 ``server.main()`` 不再用 ``uvicorn.run()``，
    改用 ``uvicorn.Config`` + ``uvicorn.Server`` ——
    演示模式要能被看板上的按钮**真的**停掉
    （``Server.should_exit = True`` 是 uvicorn 文档里的正规做法）。

    所以这个假模块必须同时提供三个名字，否则启动流程会直接崩。
    """
    module = types.ModuleType("uvicorn")

    class _FakeConfig:
        """假的 ``uvicorn.Config``。"""

        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

    class _FakeServer:
        """假的 ``uvicorn.Server``。"""

        def __init__(self, config) -> None:
            self.config = config
            self.should_exit = False

        def run(self) -> None:
            """什么都不做 —— 测试里不需要真的监听端口。"""

    module.Config = _FakeConfig
    module.Server = _FakeServer
    module.run = lambda *a, **k: None
    return module


def _run_demo_main(monkeypatch, tmp_path, argv):
    """跑一次 ``server.main()``，把终端输出与调用顺序抓回来。

    这里把 ``uvicorn`` 换成一个**假模块** ——
    服务不会真的起来，但启动流程（打大字报 → 打说明）会完整跑一遍。
    """
    from src import demo
    from src.gui import server

    calls = []
    watch = tmp_path / "demo_session.jsonl"
    watch.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        server, "print_banner", lambda *a, **k: calls.append("banner")
    )
    monkeypatch.setattr(
        server, "print_demo_notice", lambda *a, **k: calls.append("notice")
    )
    # v3.0.1：演示模式多了一步「别关窗口」的醒目块 + 反复提醒。
    monkeypatch.setattr(
        server,
        "print_keep_open_notice",
        lambda *a, **k: calls.append("keep-open"),
    )
    monkeypatch.setattr(
        server,
        "announce_demo_keep_open",
        lambda *a, **k: calls.append("reminder"),
    )
    monkeypatch.setattr(
        server, "announce_ready_when_up", lambda *a, **k: None
    )
    monkeypatch.setattr(
        server, "_build_scan_callback", lambda *a, **k: None
    )
    monkeypatch.setattr(
        demo, "generate_demo_log", lambda *a, **k: str(watch)
    )
    monkeypatch.setattr(
        demo, "DEMO_ALERT_LOG_NAME", "demo_alert_test.log"
    )

    monkeypatch.setitem(sys.modules, "uvicorn", _fake_uvicorn())

    code = server.main(argv)
    return code, calls


def test_大字报之后打印演示说明(monkeypatch, tmp_path):
    """顺序必须是「先大字报、后说明」—— 任务原话。"""
    code, calls = _run_demo_main(
        monkeypatch,
        tmp_path,
        ["--demo", "--port", "8765", "--no-browser", "--no-health"],
    )
    assert code == 0
    # v3.0.1：说明之后紧接着是「别关窗口」的块 + 反复提醒线程。
    assert calls == ["banner", "notice", "keep-open", "reminder"]


def test_真实模式不打印演示说明(monkeypatch, tmp_path):
    """真实监控**绝不能**打这段说明 —— 那会变成另一种误导。"""
    watch = tmp_path / "real.jsonl"
    watch.write_text("", encoding="utf-8")

    from src.gui import server

    calls = []
    monkeypatch.setattr(
        server, "print_banner", lambda *a, **k: calls.append("banner")
    )
    monkeypatch.setattr(
        server, "print_demo_notice", lambda *a, **k: calls.append("notice")
    )
    monkeypatch.setattr(
        server, "announce_ready_when_up", lambda *a, **k: None
    )
    monkeypatch.setattr(
        server, "_build_scan_callback", lambda *a, **k: None
    )

    fake_uvicorn = _fake_uvicorn()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    code = server.main(
        ["--watch", str(watch), "--port", "8766", "--no-browser",
         "--no-health"]
    )
    assert code == 0
    # 真实模式**绝不能**打演示相关的任何提示 ——
    # 否则那又是一句「这是模拟的」式误导。
    assert calls == ["banner"]


def test_看板源码里横幅与说明都在():
    """防「有人把 demo 标志忘传」—— 源码里必须两处都在。"""
    from src.gui import server

    source = open(server.__file__, encoding="utf-8").read()
    assert "print_demo_notice" in source
    assert "demo_mode=demo_mode" in source


def test_模板里横幅文案在():
    """模板里的横幅文案要跟终端保持同一个承诺。"""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        root, "src", "gui", "templates", "dashboard.html"
    )
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert EXPECTED_BANNER_TEXT in text
    assert "demo-banner" in text
    assert "{% if demo_mode %}" in text
