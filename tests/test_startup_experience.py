"""启动体验测试 —— v1.0.1「修BAT闪退与启动体验优化」。

覆盖四件事：

1. **终端呈现层**（:mod:`src.console`）—— ASCII 大字报、就绪提示、
   端口冲突提示、延迟自动开浏览器；
2. **演示模式**（:mod:`src.demo`）—— 生成的模拟日志必须是合法 JSONL，
   并且**真的**能触发一条红线级告警（不然演示就白演了）；
3. **看板服务入口**（:mod:`src.gui.server`）—— 参数校验、演示模式接线；
4. **Windows 启动器**（``启动哨兵.bat``）—— 欢迎语、``pause``、
   演示模式路由、行尾格式（cmd.exe 对 LF + ``goto`` 有已知的坑）。
"""

from __future__ import annotations

import io
import json
import os
import socket
from typing import Any, Dict, List

import pytest

from src import console
from src.demo import (
    DEMO_ALERT_LOG_NAME,
    DEMO_LINES,
    DEMO_TASK_GOAL,
    generate_demo_log,
)

#: 仓库根目录（本文件在 ``tests/`` 下）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Windows 启动器路径。
BAT_PATH = os.path.join(ROOT, "启动哨兵.bat")


@pytest.fixture(scope="module")
def bat_text() -> str:
    """读取 BAT 文本。"""
    with open(BAT_PATH, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# 终端呈现层
# ---------------------------------------------------------------------------
class TestBanner:
    """ASCII 大字报。"""

    def test_banner_renders_both_words(self) -> None:
        """字报里要能看出 SENTINEL 与 ACTIVE。"""
        banner = console.build_banner("SENTINEL ACTIVE")
        assert banner
        # 两个词各占 5 行，共 10 行。
        assert len(banner.splitlines()) == 10

    def test_banner_fits_narrow_console(self) -> None:
        """80 列的控制台放得下 —— 折行就难看了。"""
        banner = console.build_banner()
        assert max(len(line) for line in banner.splitlines()) <= 80

    def test_banner_uses_block_font(self) -> None:
        """用的是实心块字模，不是纯 ASCII 的 # —— 对齐才好看。"""
        assert "█" in console.build_banner()

    def test_print_banner_writes_to_stream(self) -> None:
        """字报要能写进指定流（便于测试与重定向）。"""
        stream = io.StringIO()
        console.print_banner(stream=stream)
        assert "█" in stream.getvalue()

    def test_empty_text_yields_nothing(self) -> None:
        """空文本不该打出空行。"""
        assert console.build_banner("") == ""


class TestReadyAndBusyMessages:
    """就绪提示与端口冲突提示。"""

    def test_ready_message_contains_url(self) -> None:
        """就绪提示必须带看板地址。"""
        stream = io.StringIO()
        console.print_ready("127.0.0.1", 8000, stream=stream)
        text = stream.getvalue()
        assert "[看板已就绪]" in text
        assert "http://127.0.0.1:8000" in text
        assert "查看实时审计" in text

    def test_port_busy_message_is_human_readable(self) -> None:
        """端口冲突要给一句人话，而不是让人自己看异常栈。"""
        stream = io.StringIO()
        console.print_port_busy("127.0.0.1", 8000, stream=stream)
        text = stream.getvalue()
        assert "端口被占用" in text
        assert "修改端口" in text

    def test_port_busy_constant_matches_requirement(self) -> None:
        """提示语要与需求里给的文案一致。"""
        assert console.PORT_BUSY_MESSAGE == "端口被占用，请修改端口或关闭已有进程。"


class TestPortProbing:
    """端口探测。"""

    def test_free_port_is_detected(self) -> None:
        """空闲端口要能测出来。"""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        assert console.port_is_free("127.0.0.1", port) is True

    def test_occupied_port_is_detected(self) -> None:
        """被占用的端口要能测出来（不然启动时只会炸异常栈）。"""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            assert console.port_is_free("127.0.0.1", port) is False
        finally:
            sock.close()

    def test_invalid_port_is_not_free(self) -> None:
        """非法端口一律当成「不可用」。"""
        assert console.port_is_free("127.0.0.1", 0) is False
        assert console.port_is_free("127.0.0.1", 999999) is False
        assert console.port_is_free("127.0.0.1", "abc") is False


class TestBrowserOpening:
    """自动开浏览器。"""

    def test_delayed_open_calls_opener(self) -> None:
        """延迟到期后要真的去开。"""
        opened: List[str] = []
        timer = console.open_browser_delayed(
            "http://127.0.0.1:8000", delay=0.05, opener=opened.append
        )
        assert timer is not None
        timer.join(timeout=3)
        assert opened == ["http://127.0.0.1:8000"]

    def test_empty_url_does_nothing(self) -> None:
        """没有地址就不该开浏览器。"""
        opened: List[str] = []
        assert console.open_browser_delayed("", opener=opened.append) is None
        assert not opened

    def test_opener_failure_does_not_raise(self) -> None:
        """开浏览器失败**不能**把启动流程带崩。"""

        def _boom(_url: str) -> None:
            raise RuntimeError("没有默认浏览器")

        # 不抛异常就是通过。
        console.open_browser_delayed(
            "http://127.0.0.1:8000", delay=0, opener=_boom
        )

    def test_announce_waits_for_port_then_opens(self) -> None:
        """就绪提示与开浏览器，都要等端口**真的**能连上。"""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        opened: List[str] = []
        try:
            thread = console.announce_ready_when_up(
                "127.0.0.1",
                port,
                timeout=5.0,
                interval=0.05,
                open_browser=True,
                browser_delay=0.05,
                opener=opened.append,
            )
            thread.join(timeout=10)
        finally:
            sock.close()
        assert opened == [f"http://127.0.0.1:{port}"]

    def test_announce_gives_up_when_port_never_opens(self) -> None:
        """端口一直起不来时也不能死等 —— 超时就收手。"""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        opened: List[str] = []
        thread = console.announce_ready_when_up(
            "127.0.0.1",
            port,
            timeout=0.4,
            interval=0.05,
            open_browser=True,
            browser_delay=0.01,
            opener=opened.append,
        )
        thread.join(timeout=10)
        assert not opened


# ---------------------------------------------------------------------------
# 演示模式
# ---------------------------------------------------------------------------
class TestDemoLog:
    """演示用的模拟日志。"""

    def test_generate_demo_log_creates_file(self, tmp_path: Any) -> None:
        """要真的生成文件。"""
        path = generate_demo_log(str(tmp_path))
        assert os.path.isfile(path)
        assert path.endswith("demo_session.jsonl")

    def test_demo_log_is_valid_jsonl(self, tmp_path: Any) -> None:
        """每一行都得是合法 JSON —— 否则适配器解析不了。"""
        path = generate_demo_log(str(tmp_path))
        with open(path, encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        assert len(lines) == len(DEMO_LINES)
        for line in lines:
            assert isinstance(json.loads(line), dict)

    def test_demo_log_is_overwritten_each_time(self, tmp_path: Any) -> None:
        """每次演示都从干净状态开始 —— 否则看板会堆旧告警。"""
        path = generate_demo_log(str(tmp_path))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type": "thinking", "thinking": "脏数据"}\n')
        generate_demo_log(str(tmp_path))
        with open(path, encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        assert len(lines) == len(DEMO_LINES)

    def test_demo_log_triggers_red_line_alert(self, tmp_path: Any) -> None:
        """演示必须能亮出**红线卡片** —— 这正是要给用户看的东西。"""
        from src.adapters.mcode import McodeAdapter
        from src.config_loader import load_config
        from src.rules.basic import RuleEngine

        path = generate_demo_log(str(tmp_path))
        adapter = McodeAdapter()
        engine = RuleEngine(load_config(None))
        red_lines: List[Dict[str, Any]] = []
        with open(path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                for event in adapter.parse_payload(json.loads(line)):
                    verdict = engine.check_event(event)
                    if verdict.get("severity") == "red_line":
                        red_lines.append(verdict)
        assert red_lines, "演示日志里必须至少有一条红线级判定"
        assert red_lines[0]["rule_id"] == "catastrophic_mass_delete"

    def test_demo_task_goal_is_set(self) -> None:
        """演示也要有任务目标 —— 看板的「当前监控目标」要显示得出东西。"""
        assert DEMO_TASK_GOAL

    def test_demo_alert_log_name(self) -> None:
        """演示告警日志有固定名字，便于清理。"""
        assert DEMO_ALERT_LOG_NAME.endswith(".log")


# ---------------------------------------------------------------------------
# 看板服务入口
# ---------------------------------------------------------------------------
class TestServerEntry:
    """``python -m src.gui.server`` 的命令行入口。"""




# ---------------------------------------------------------------------------
# Windows 启动器
# ---------------------------------------------------------------------------
class TestLauncherBat:
    """``启动哨兵.bat`` 的静态契约。

    BAT 没法 import，所以这里直接读文件做断言 ——
    「会不会闪退」「有没有欢迎语」全靠它。
    """

    def test_bat_exists(self) -> None:
        """启动器必须在根目录。"""
        assert os.path.isfile(BAT_PATH)

    def test_bat_has_welcome_block(self, bat_text: str) -> None:
        """启动前要有醒目的欢迎信息。"""
        assert "🛡️ 哨兵系统正在启动..." in bat_text
        assert "========================================" in bat_text

    def test_bat_pauses_at_the_end(self, bat_text: str) -> None:
        """无论成功失败都要 pause —— 窗口不能闪退。"""
        assert "pause" in bat_text
        # pause 必须出现在「统一出口」那段里，也就是最后一个可执行段。
        assert bat_text.rstrip().lower().endswith("exit /b %exitcode%")
        assert bat_text.lower().rindex("pause") > (
            bat_text.lower().rindex(":finish")
        )

    def test_bat_reports_missing_python(self, bat_text: str) -> None:
        """找不到 Python 要给看得懂的中文提示。"""
        assert "未找到 Python" in bat_text
        assert ":no_python" in bat_text

    def test_bat_reports_dependency_failure(self, bat_text: str) -> None:
        """依赖装不上也要给中文提示。"""
        assert "依赖安装失败" in bat_text
        assert ":dep_failed" in bat_text

    def test_bat_routes_no_args_to_start_menu(self, bat_text: str) -> None:
        """不带参数 → 走入口脚本，由它打印启动菜单（v3.0.2）。

        ⚠️ v3.0.1 这里是直接 ``--demo`` —— 用户以为「雷达扫不到真实的
        进程」，因为哨兵**压根没问他要干什么**就替他决定了。
        """
        assert 'if "%~1"==""' in bat_text
        assert "sentinel_entry.py" in bat_text
        # 旧的「直接进演示」必须清干净
        assert "-m src.gui.server --demo" not in bat_text

    def test_bat_passes_through_arguments(self, bat_text: str) -> None:
        """带参数 → 透传给真实接管脚本。"""
        assert "run_with_sentinel.py" in bat_text
        assert "%*" in bat_text

    def test_bat_has_no_delayed_expansion(self, bat_text: str) -> None:
        """延迟扩展会把 ``[!]`` 吃成 ``[]`` —— 本脚本用不着它。

        变量都在块外赋值、用的是 ``%VAR%`` 而不是 ``!VAR!``，
        所以关掉延迟扩展既安全又能让 ``[!]`` 正常显示。

        只看**代码行** —— 注释里可以解释这件事。
        """
        code_lines = [
            line.strip()
            for line in bat_text.splitlines()
            if line.strip() and not line.strip().lower().startswith("rem")
        ]
        assert not any(
            "enabledelayedexpansion" in line.lower() for line in code_lines
        )
        assert "[!]" in bat_text

    def test_bat_uses_crlf_line_endings(self) -> None:
        """cmd.exe 的 goto / 标签在 LF 行尾下会失效 —— 必须是 CRLF。"""
        with open(BAT_PATH, "rb") as handle:
            raw = handle.read()
        assert b"\r\n" in raw
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_bat_has_no_bom(self) -> None:
        """带 BOM 的 BAT 在部分 Windows 上会解析出错。"""
        with open(BAT_PATH, "rb") as handle:
            raw = handle.read()
        assert not raw.startswith(b"\xef\xbb\xbf")
