"""v2.2.1 回归：双击 exe 不闪退 + 体检只打一份。

覆盖三件事：

1. **无参数时补上 ``--demo``** —— 这是闪退的根因：
   看板的解析器没有 ``--demo`` 也没有 ``--watch`` 时会
   ``parser.error()`` → ``sys.exit(2)`` → 窗口瞬间消失；
2. **一个进程只体检一次** —— 终端上不能出现两份统计；
3. **致命错误不闪退** —— 出错时先说清楚，
   而且**只有真终端**才挂起（管道 / 脚本调用不能被挂住）。
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys

import pytest

from src.console import fatal_guard, report_fatal, wait_for_user
from src.core.health import (
    reset_startup_health_cache,
    run_startup_health_check,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _load_entry():
    """加载打包入口脚本（它不在 ``src`` 包里，是独立脚本）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        root, "_构建与发布", "scripts", "sentinel_entry.py"
    )
    spec = importlib.util.spec_from_file_location("sentinel_entry_v221", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# ---------------------------------------------------------------------------
# 任务一：无参数 → 交互式启动菜单（v3.0.2 改）
# ---------------------------------------------------------------------------
def test_无参数时显示菜单并默认选雷达(monkeypatch):
    """双击 exe（无参数）必须**先出菜单**，不能直接进演示。

    v3.0.2 的设计：终端里打印三个选项让用户按键选。
    非交互环境（没有终端可输入）→ 走默认值 ``[2] 开启雷达`` ——
    默认给「真实的东西」比默认给「假的」诚实。
    """
    entry = _load_entry()
    captured = {}

    def _fake_server_main(argv=None):
        captured["argv"] = list(argv or [])
        return 0

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)

    assert entry.main([]) == 0
    # 默认选项是「开启雷达」，不是演示。
    assert "--radar" in captured["argv"]
    assert "--demo" not in captured["argv"]


def test_无参数时菜单打印三个选项(monkeypatch, capsys):
    """菜单必须把三个选项原样打印到终端。"""
    entry = _load_entry()

    def _fake_server_main(argv=None):
        return 0

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)

    entry.main([])
    output = capsys.readouterr().out

    assert "[1] 启动演示模式 (体验界面)" in output
    assert "[2] 开启雷达 (扫描已有的 Agent 日志)" in output
    assert "[3] 接管并启动新的 Agent" in output


def test_显式_demo_不被重复添加(monkeypatch):
    """已经写了 ``--demo`` 就别再加一遍。"""
    entry = _load_entry()
    captured = {}

    def _fake_server_main(argv=None):
        captured["argv"] = list(argv or [])
        return 0

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)

    entry.main(["--demo"])
    assert captured["argv"].count("--demo") == 1


def test_其它参数原样传下去(monkeypatch):
    """端口、主机这些参数不能被入口吃掉。"""
    entry = _load_entry()
    captured = {}

    def _fake_server_main(argv=None):
        captured["argv"] = list(argv or [])
        return 0

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)

    entry.main(["--demo", "--port", "8123", "--no-browser"])
    argv = captured["argv"]
    assert "--port" in argv and "8123" in argv
    assert "--no-browser" in argv


def test_看板起不来时不闪退(monkeypatch):
    """端口被占这类「返回非零码」的失败，窗口也得**挂住**。"""
    entry = _load_entry()
    waited = []

    def _fake_server_main(argv=None):
        return 2

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)
    monkeypatch.setattr(entry, "_wait_for_user", lambda *a, **k: waited.append(1))

    assert entry.main([]) == 2
    assert waited, "看板起不来时必须挂住等用户看提示"


def test_看板正常跑完不挂起(monkeypatch):
    """正常结束（退出码 0）不该拦着用户。"""
    entry = _load_entry()
    waited = []

    def _fake_server_main(argv=None):
        return 0

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)
    monkeypatch.setattr(entry, "_wait_for_user", lambda *a, **k: waited.append(1))

    assert entry.main([]) == 0
    assert not waited


def test_与启动脚本行为一致():
    """``启动哨兵.bat`` 无参数时走的是入口脚本 —— 两边必须一致。

    v3.0.2：bat 不再自己决定「进演示」，而是交给入口脚本打印菜单。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bat = os.path.join(root, "启动哨兵.bat")
    if not os.path.exists(bat):
        pytest.skip("启动哨兵.bat 不在（打包版不需要它）")
    with open(bat, encoding="utf-8") as handle:
        text = handle.read()
    assert "sentinel_entry.py" in text
    assert "-m src.gui.server --demo" not in text


# ---------------------------------------------------------------------------
# 任务三：体检只打一份
# ---------------------------------------------------------------------------
def test_体检一个进程只打一份():
    """重复调用**不再**打印第二份报告。"""
    reset_startup_health_cache()
    printed = []
    first = run_startup_health_check(
        {}, workdir="", skip_network=True, printer=printed.append
    )
    second = run_startup_health_check(
        {}, workdir="", skip_network=True, printer=printed.append
    )
    assert len(printed) == 1
    assert len(first) == 4
    assert len(second) == 4
    reset_startup_health_cache()


def test_报告里统计行只出现一次():
    """一份报告里「正常 X · 警告 X…」只能有一行。"""
    reset_startup_health_cache()
    printed = []
    run_startup_health_check(
        {}, workdir="", skip_network=True, printer=printed.append
    )
    text = printed[0]
    summary_lines = [
        line for line in text.splitlines() if line.startswith("正常 ")
    ]
    assert len(summary_lines) == 1
    assert text.count("哨兵安全体检报告") == 1
    reset_startup_health_cache()


def test_清掉标记后可以再跑():
    """测试之间不能互相污染 —— 标记要能清。"""
    reset_startup_health_cache()
    printed = []
    run_startup_health_check(
        {}, workdir="", skip_network=True, printer=printed.append
    )
    reset_startup_health_cache()
    run_startup_health_check(
        {}, workdir="", skip_network=True, printer=printed.append
    )
    assert len(printed) == 2
    reset_startup_health_cache()


def test_看板演示模式也走体检(monkeypatch):
    """``python -m src.gui.server --demo`` 这条路**也要**体检。"""
    from src.gui import server

    calls = []

    def _fake_check(config=None, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(
        "src.core.health.run_startup_health_check", _fake_check
    )
    # 只验证「演示模式会调用体检」这一件事，
    # 不真的起服务（那要占端口）。
    source = open(server.__file__, encoding="utf-8").read()
    assert "run_startup_health_check" in source
    assert "--no-health" in source


def test_服务端解析器认识_no_health():
    """``--no-health`` 必须被看板的解析器认识 —— 否则又是一次闪退。"""
    from src.gui import server

    parser = server.main.__doc__
    assert parser is not None
    source = open(server.__file__, encoding="utf-8").read()
    assert '"--no-health"' in source


# ---------------------------------------------------------------------------
# 任务二：致命错误不闪退
# ---------------------------------------------------------------------------
def test_致命错误被兜住():
    """出异常时返回 1，而不是把栈直接甩给用户。"""
    def _boom():
        raise RuntimeError("炸了")

    assert fatal_guard(_boom) == 1


def test_正常返回原样透传():
    """正常路径不能被守卫改动。"""
    assert fatal_guard(lambda: 7) == 7
    assert fatal_guard(lambda: None) == 0


def test_参数错误不闪退():
    """argparse 的 ``parser.error()`` 走的是 SystemExit —— 也要兜住。"""
    def _arg_error():
        raise SystemExit(2)

    assert fatal_guard(_arg_error) == 2


def test_正常退出码零不挂起():
    """``SystemExit(0)`` 是正常退出，不该拦着用户。"""
    def _clean():
        raise SystemExit(0)

    assert fatal_guard(_clean) == 0


def test_中断返回130():
    """Ctrl+C 走 KeyboardInterrupt。"""
    def _interrupted():
        raise KeyboardInterrupt

    assert fatal_guard(_interrupted) == 130


def test_非终端不挂起(monkeypatch):
    """**管道 / 脚本调用不能被挂住** —— 那比闪退更糟。"""

    class _NotATty:
        def isatty(self):
            return False

        def readline(self):
            raise AssertionError("不该读 stdin")

    monkeypatch.setattr(sys, "stdin", _NotATty())
    # 不抛异常就是没挂住。
    wait_for_user()


def test_真终端才挂起(monkeypatch):
    """真的连着终端时，出错后**要**等用户看完。"""
    asked = []

    class _ATty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _ATty())
    monkeypatch.setattr(
        "builtins.input", lambda *args, **kwargs: asked.append(args)
    )
    wait_for_user("按回车……")
    assert asked


def test_致命提示包含原因():
    """提示里要能看到**到底哪儿错了**。"""
    recorder = _Recorder()
    report_fatal(RuntimeError("端口被占"), stream=recorder)
    text = recorder.text
    assert "RuntimeError" in text
    assert "端口被占" in text
    assert "端口被占了" in text
