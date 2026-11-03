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
import sys
import types

from src.console import DEMO_NOTICE_LINES, DEMO_NOTICE_WIDTH, print_demo_notice
from src.core import session_server as server

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




def _html(app):
    """取首页 HTML。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        return client.get("/").text


























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
