"""终端呈现层 —— ASCII 大字报、看板就绪提示、端口冲突提示、自动开浏览器。

为什么要单独成模块
==================
「在终端上给人看好看的提示」这件事，**看板服务**（:mod:`src.gui.server`）
和**启动器脚本**（``run_with_sentinel.py``）**都要**做。
各写一份的话，两边很快就会长得不一样 —— 一个打印了、另一个没打印。

所以把它收成**叶子模块** ``src/console.py``：
只用标准库，**不依赖哨兵的任何内部模块**。
这样应用层（GUI）import 它时不会把 ``src.core.engine`` 拖进来，
「GUI 不认识引擎」这条解耦原则不受影响。

对外只暴露四件事
================

* :func:`build_banner` / :func:`print_banner` —— ASCII 大字报；
* :func:`print_ready` —— 看板就绪提示（附看板地址）；
* :func:`print_port_busy` —— 端口被占用的**友好**提示；
* :func:`open_browser_delayed` —— 延迟自动打开浏览器。

另外三件（v2.2.1 新增）：

* :func:`report_fatal` —— 致命错误的**人话**提示；
* :func:`wait_for_user` —— 出错后**挂住**等用户看完（只在真终端时）；
* :func:`fatal_guard` —— 把上面两件事包成一层「**绝不闪退**」的外壳。

再一件（v2.2.2 新增）：

* :func:`print_demo_notice` —— 演示模式的**醒目说明**（6 行，逐字固定）。

再一件（v3.0.0 新增）：

* :func:`print_greeting` —— **极简启动问候**（一行：``Sentinel v3.0.0 is ready.``）。

⚠️ 版本号为什么是**参数**而不是模块内取
======================================

本模块是**叶子模块** —— 只用标准库，不 import 哨兵的任何内部模块。
所以它**不知道**当前版本号，得由调用方（知道版本的地方）传进来。
这既保住了「GUI 不认识引擎」的解耦原则，
也让「问候里的版本号」和「命令行 ``--version`` 打印的版本号」**必然一致** ——
两边都来自 :data:`src.__version__`。

⚠️ 为什么演示模式一定要**说破**
=============================

v2.2.1 之前，用户双击 ``sentinel.exe`` 看到的是一屏告警卡片，
其中就包括「删除文件」这种**假红线**。他完全有理由以为：
「哨兵监控到了我正在独立运行的那个 Agent。」

这叫**认知欺骗** —— 界面没说谎，但界面**没说全**，
用户就会按错误的假设理解一切。所以演示模式必须**显式声明**：
看到的一切都是模拟数据，**没有监控任何真实进程**。

为什么大字报要分两行
====================
``SENTINEL ACTIVE`` 连排是 112 个字符 —— 在 80 列的控制台里会被折成两截，
反而更难看。所以按**词**换行：``SENTINEL`` 一行、``ACTIVE`` 一行，
最宽 63 列，任何控制台都放得下。
"""

from __future__ import annotations

import logging
import os
import socket
import stat as stat_module
import sys
import threading
import time
import webbrowser
from typing import Any, Callable, Dict, Optional, Tuple

#: 本模块的日志器 —— 终端呈现层出问题时，这里是最后的留痕渠道。
_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BANNER_TEXT",
    "DEFAULT_BROWSER_DELAY",
    "DEMO_KEEP_OPEN_LINES",
    "DEMO_NOTICE_LINES",
    "DEMO_NOTICE_WIDTH",
    "DEMO_REMINDER_INTERVAL",
    "DEMO_REMINDER_LINE",
    "DEMO_REMINDER_TIMES",
    "FATAL_HINTS",
    "GREETING_MESSAGE",
    "PORT_BUSY_MESSAGE",
    "RADAR_KEEP_OPEN_LINES",
    "RADAR_REMINDER_LINE",
    "READY_MESSAGE",
    "START_MENU_CHOICES",
    "START_MENU_DEFAULT",
    "START_MENU_LINES",
    "START_MENU_WIDTH",
    "announce_demo_keep_open",
    "announce_radar_keep_open",
    "announce_ready_when_up",
    "build_banner",
    "fatal_guard",
    "open_browser_delayed",
    "port_is_accepting",
    "port_is_free",
    "print_banner",
    "print_demo_notice",
    "print_greeting",
    "print_keep_open_notice",
    "print_port_busy",
    "print_radar_keep_open_notice",
    "print_ready",
    "print_start_menu",
    "read_start_choice",
    "report_fatal",
    "wait_for_user",
]

#: 大字报默认文字。
BANNER_TEXT = "SENTINEL ACTIVE"

#: 看板就绪提示的模板（``{host}`` / ``{port}`` 会被替换）。
READY_MESSAGE = "[看板已就绪] 请访问 http://{host}:{port} 查看实时审计。"

#: 极简启动问候的模板（``{version}`` 会被替换）—— v3.0.0。
GREETING_MESSAGE = "Sentinel v{version} is ready."

#: 端口被占用时的提示。
PORT_BUSY_MESSAGE = "端口被占用，请修改端口或关闭已有进程。"

#: 自动打开浏览器的默认延迟（秒）—— 要等服务真的监听上了。
DEFAULT_BROWSER_DELAY = 1.5

#: 字模高度（行数）。
_FONT_HEIGHT = 5

#: 字模宽度（每行字符数）。
_FONT_WIDTH = 7

#: ASCII 字模 —— 每个字母 5 行、每行 7 个字符。
#:
#: 用 ``█``（U+2588 实心块）拼块状字。选它是因为它在
#: Windows 控制台（chcp 65001）与多数终端里都能对齐，
#: 用 ``#`` 拼会因字体而参差不齐。
_FONT: Dict[str, Tuple[str, ...]] = {
    "A": (" █████ ", "██   ██", "███████", "██   ██", "██   ██"),
    "C": (" ██████", "██     ", "██     ", "██     ", " ██████"),
    "E": ("███████", "██     ", "█████  ", "██     ", "███████"),
    "I": ("███████", "  ███  ", "  ███  ", "  ███  ", "███████"),
    "L": ("██     ", "██     ", "██     ", "██     ", "███████"),
    "N": ("███  ██", "████ ██", "█ ████", "█  ███", "█   ██"),
    "S": (" █████ ", "██     ", " ████  ", "     ██", "████   "),
    "T": ("███████", "  ███  ", "  ███  ", "  ███  ", "  ███  "),
    "V": ("██   ██", "██   ██", "██   ██", " ██ ██ ", "  ███  "),
}

#: 词与词之间的间隔（列）。
_WORD_GAP = " "


def _blank_glyph() -> Tuple[str, ...]:
    """返回一张空白字模（用于空格与未知字符）。"""
    return tuple(" " * _FONT_WIDTH for _ in range(_FONT_HEIGHT))


def _glyph(char: str) -> Tuple[str, ...]:
    """取一个字符的字模。

    :param char: 单个字符（大小写不敏感）。
    :return: 5 行、每行 7 字符的字模；未知字符返回空白。
    """
    return _FONT.get(char.upper(), _blank_glyph())


def build_banner(text: str = BANNER_TEXT) -> str:
    """把文字渲染成 ASCII 大字报。

    按**词**换行 —— 一行太长会在窄控制台里被折断。

    :param text: 要渲染的文字，默认 ``SENTINEL ACTIVE``。
    :return: 多行文本（不含行尾换行）。
    """
    words = [word for word in str(text or "").upper().split() if word]
    if not words:
        return ""
    lines = []
    for word in words:
        glyphs = [_glyph(char) for char in word]
        lines.extend(
            _WORD_GAP.join(glyph[row] for glyph in glyphs)
            for row in range(_FONT_HEIGHT)
        )
    return "\n".join(lines)


def print_banner(text: str = BANNER_TEXT, stream: Any = None) -> None:
    """在终端打印 ASCII 大字报。

    :param text: 要渲染的文字。
    :param stream: 输出流；默认 ``sys.stdout``。
    """
    import sys

    target = stream if stream is not None else sys.stdout
    banner = build_banner(text)
    if not banner:
        return
    print(banner, file=target, flush=True)


def print_ready(host: str, port: int, stream: Any = None) -> None:
    """打印「看板已就绪」提示。

    :param host: 看板监听地址。
    :param port: 看板监听端口。
    :param stream: 输出流；默认 ``sys.stdout``。
    """
    import sys

    target = stream if stream is not None else sys.stdout
    print(READY_MESSAGE.format(host=host, port=port), file=target, flush=True)


def print_greeting(version: str, stream: Any = None) -> None:
    """打印**极简启动问候**（v3.0.0）。

    就一行：``Sentinel v3.0.0 is ready.``

    为什么只有一行
    =============

    哨兵启动时终端上已经有**一堆**东西了 —— ASCII 大字报、演示模式说明、
    看板地址、开箱体检报告…… 再加一句长的只会更吵。
    这一行只回答用户最想知道的那件事：**好了没有？**

    :param version: 版本号（形如 ``3.0.0``）。
    :param stream: 输出流；默认 ``sys.stdout``。
    """
    import sys

    target = stream if stream is not None else sys.stdout
    try:
        print(
            GREETING_MESSAGE.format(version=version),
            file=target,
            flush=True,
        )
    except (OSError, ValueError) as exc:
        # 管道被关了 / 编码装不下 —— **绝不能**因此带崩启动（红线 R5）。
        # 但要留痕：否则「用户没看到问候」会变成一个查不出来的谜。
        _LOGGER.debug("打印启动问候失败：%s: %s", type(exc).__name__, exc)


def print_port_busy(host: str, port: int, stream: Any = None) -> None:
    """打印「端口被占用」的友好提示。

    **不要**直接抛红字异常栈 —— 用户看到的是「端口被占用，
    请修改端口或关闭已有进程」，不是一屏 traceback。

    :param host: 打算监听的地址。
    :param port: 打算监听的端口。
    :param stream: 输出流；默认 ``sys.stdout``。
    """
    import sys

    target = stream if stream is not None else sys.stdout
    print(f"[{host}:{port}] {PORT_BUSY_MESSAGE}", file=target, flush=True)


def port_is_free(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """检查端口能不能用。

    做法是**真去 bind 一下**再放开 —— 比读系统端口表可靠
    （端口表可能过期，而且看不出是谁占的）。

    ⚠️ 刻意**不**设置 ``SO_REUSEADDR``：Windows 上设了它反而可能
    在端口已被占用时也能 bind 成功，那样就检测不出来了。

    :param host: 地址。
    :param port: 端口。
    :return: 端口空闲返回 ``True``。
    """
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return False
    if port_number <= 0 or port_number > 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((str(host or "127.0.0.1"), port_number))
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError as exc:
            # 关不掉一个**刚绑定完**的探测套接字 —— 不影响结论。
            _LOGGER.debug(
                "关闭探测套接字失败：%s: %s", type(exc).__name__, exc
            )
    return True


def open_browser_delayed(
    url: str,
    delay: float = DEFAULT_BROWSER_DELAY,
    opener: Optional[Callable[[str], Any]] = None,
) -> Optional[threading.Timer]:
    """延迟打开浏览器。

    **为什么要延迟**：服务刚起时端口还在 listen 过程中，
    立刻打开会撞上「连接被拒绝」。等 1.5 秒比较稳。

    :param url: 要打开的地址。
    :param delay: 延迟秒数；``<= 0`` 表示立刻开。
    :param opener: 打开函数（测试用）；默认 :func:`webbrowser.open`。
    :return: 已启动的定时器；地址为空时返回 ``None``。
    """
    target = str(url or "").strip()
    if not target:
        return None
    action = opener if opener is not None else webbrowser.open

    def _open() -> None:
        """真正打开浏览器 —— 失败只记一行，不该把启动流程带崩。"""
        try:
            action(target)
        except Exception as exc:  # noqa: BLE001 —— 开浏览器失败不影响服务
            print(f"[SENTINEL] 打开浏览器失败（{type(exc).__name__}: {exc}）。")
            print(f"[SENTINEL] 请手动访问：{target}")

    if delay and delay > 0:
        timer = threading.Timer(delay, _open)
        timer.daemon = True
        timer.start()
        return timer
    _open()
    return None


def port_is_accepting(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """试着连一下端口 —— 连得上就说明服务**真的**起来了。

    :param host: 地址。
    :param port: 端口。
    :return: 端口能接受连接返回 ``True``。
    """
    try:
        target = (str(host or "127.0.0.1"), int(port))
        with socket.create_connection(target, 0.3):
            return True
    except (OSError, ValueError, TypeError):
        return False


def announce_ready_when_up(
    host: str,
    port: int,
    *,
    version: str = "",
    timeout: float = 20.0,
    interval: float = 0.2,
    open_browser: bool = True,
    browser_delay: float = DEFAULT_BROWSER_DELAY,
    opener: Optional[Callable[[str], Any]] = None,
) -> threading.Thread:
    """后台等端口就绪，然后打印提示并（可选）打开浏览器。

    **为什么不直接在启动前打印「已就绪」？**
    因为那一刻服务**还没**监听上 —— 说了「已就绪」再让人点链接，
    很可能撞上「连接被拒绝」。这里改成**真的连一下**，
    连上了才敢说「已就绪」，浏览器也在这之后才开。

    :param host: 看板监听地址。
    :param port: 看板监听端口。
    :param version: 版本号 —— 给了就顺手打一行极简问候（v3.0.0）。
    :param timeout: 最长等多久（秒）。
    :param interval: 轮询间隔（秒）。
    :param open_browser: 是否自动打开浏览器。
    :param browser_delay: 打开浏览器的额外延迟（秒）。
    :param opener: 打开函数（测试用）。
    :return: 已启动的后台线程。
    """
    url = f"http://{host}:{port}"

    def _worker() -> None:
        """轮询端口 → 就绪后提示 + 开浏览器。"""
        deadline = time.monotonic() + max(float(timeout), 0.0)
        ready = False
        while time.monotonic() < deadline:
            if port_is_accepting(host, port):
                ready = True
                break
            time.sleep(max(float(interval), 0.05))
        if not ready:
            print(f"[SENTINEL] 等待看板端口 {host}:{port} 超时。")
            print(f"[SENTINEL] 请手动访问：{url}")
            return
        print_ready(host, port)
        if version:
            # 极简问候 —— 只在**真的**连上了之后才说（v3.0.0）。
            print_greeting(version)
        if open_browser:
            timer = open_browser_delayed(url, browser_delay, opener=opener)
            if timer is not None:
                # 等浏览器真的开完再收工 —— 这样「线程结束」就等于
                # 「浏览器已经开了」，调用方不必再猜。
                timer.join()

    thread = threading.Thread(
        target=_worker, name="sentinel-ready", daemon=True
    )
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# 致命错误：**绝不闪退**（v2.2.1）
# ---------------------------------------------------------------------------

#: 致命错误后面附的「常见原因」提示。
FATAL_HINTS = (
    "端口被占了 —— 换个端口再试：sentinel --port 8001",
    "参数写错了 —— 看一眼用法：sentinel --help",
    "依赖没装全 —— 看 logs/ 目录里的日志",
)


def report_fatal(
    exc: BaseException,
    *,
    title: str = "哨兵没能正常启动",
    hints: Optional[Tuple[str, ...]] = None,
    stream: Any = None,
) -> None:
    """把致命错误说成**人话**。

    ⚠️ 为什么要有这个
    ================

    双击 ``sentinel.exe`` 时，程序跑在一个**会被自动关掉**的窗口里。
    一次未捕获的异常 = 一屏红字 + 窗口**瞬间消失** ——
    用户连报错都来不及看，那比不做功能还糟糕。

    所以出错时先说清楚「哪儿错了、接下来怎么办」，
    再由 :func:`wait_for_user` 把窗口**挂住**。

    :param exc: 异常对象。
    :param title: 标题。
    :param hints: 常见原因提示；``None`` 时用 :data:`FATAL_HINTS`。
    :param stream: 输出流；``None`` 时用 stdout。
    """
    out = stream if stream is not None else sys.stdout
    lines = [
        "",
        "=" * 62,
        "  [!] {}".format(title),
        "=" * 62,
        "  原因：{}: {}".format(type(exc).__name__, exc),
        "",
        "  常见原因：",
    ]
    lines.extend(
        "    · {}".format(hint)
        for hint in (hints if hints is not None else FATAL_HINTS)
    )
    lines += [
        "",
        "  窗口**不会自己关** —— 看清楚上面的提示再按回车退出。",
        "=" * 62,
        "",
    ]
    try:
        out.write("\n".join(lines) + "\n")
        flush = getattr(out, "flush", None)
        if flush is not None:
            flush()
    except Exception as write_exc:  # noqa: BLE001 —— 提示都打不出来也只能算了
        # 输出流本身坏了，print 未必能落到任何地方 ——
        # 走 logging（默认写 stderr），至少留下「这里出过事」的痕迹。
        # ⚠️ 变量别叫 ``exc`` —— 外层还有个同名参数（原始异常），
        # 遮蔽了就分不清是「哪个异常写不出去」了。
        _LOGGER.warning(
            "写入提示信息失败：%s: %s",
            type(write_exc).__name__,
            write_exc,
        )


def wait_for_user(
    message: str = "程序发生错误，按回车键退出……",
    stream: Any = None,
) -> None:
    """出错后**挂住**，等用户看完再走。

    ⚠️ 只有在**真的连着终端**时才等。
    被管道 / 脚本调用时如果也等，调用方会被**挂住** ——
    那比闪退更糟。所以这里判的是 ``sys.stdin.isatty()``。

    :param message: 等待提示语。
    :param stream: 输出流；``None`` 时用 stdout。
    """
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except (AttributeError, ValueError, OSError):
        return
    out = stream if stream is not None else sys.stdout
    try:
        out.write("\n" + message + "\n")
        flush = getattr(out, "flush", None)
        if flush is not None:
            flush()
        input()
    except (EOFError, KeyboardInterrupt, OSError, ValueError) as exc:
        # 等不到人按回车（管道 / 非交互）—— 不该拦着程序退出。
        _LOGGER.debug(
            "等待用户回车失败：%s: %s", type(exc).__name__, exc
        )


def fatal_guard(
    func: Callable[..., Any],
    *args: Any,
    wait: bool = True,
    **kwargs: Any,
) -> int:
    """把一个入口包成「**绝不闪退**」的形状。

    处理三类情况：

    * **正常返回** → 原样返回退出码；
    * **SystemExit**（argparse 的参数错误走这条路）
      → 退出码非 0 时先挂住再退；
    * **其它异常** → 打印人话提示，挂住，返回 1。

    ⚠️ ``wait=False`` —— **被脚本调用时不要挂住**
    ==========================================
    「挂住等回车」是给**看得见窗口的人**用的。
    被管道 / 脚本 / CI 调用时没有人会来按回车，
    挂在那儿等 = 调用方**永远等不到返回**。

    ⚠️ v3.3.0 起，哨兵**不再有隐藏窗口运行的路径**
    （「开机自启」与「守护模式」都被删了），
    所以本项目里的入口一律 ``wait=True``。

    :param func: 要包的函数。
    :param args: 位置参数。
    :param wait: 出错后要不要挂住等用户看完。
    :param kwargs: 关键字参数。
    :return: 进程退出码。
    """
    try:
        return int(func(*args, **kwargs) or 0)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code == 0:
            return 0
        # 参数错误也要**看得见** —— 它同样会让窗口闪退。
        if wait:
            wait_for_user()
        return code
    except KeyboardInterrupt:
        print("\n[SENTINEL] 已中断。", flush=True)
        return 130
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底，绝不留裸栈
        report_fatal(exc)
        if wait:
            wait_for_user()
        return 1


# ---------------------------------------------------------------------------
# 演示模式：把「这是模拟数据」说破（v2.2.2）
# ---------------------------------------------------------------------------

#: 演示模式说明的分隔线宽度（``=`` 的个数）。
DEMO_NOTICE_WIDTH = 50

#: 演示模式的醒目说明 —— **逐字固定**，不要随手改写。
#:
#: ⚠️ 这段话是产品的**诚实性契约**：
#: 演示模式下看板里的一切（告警、日志、Token 统计）都是编出来的。
#: 用户如果误以为「哨兵在监控我正在跑的 Agent」，
#: 就会把假的红线当成真的事故 —— 那比没有演示功能更糟。
#:
#: 所以这里用**两条分隔线**把它单独框出来，
#: 并且明确给出「想监控真实 Agent 该怎么敲命令」。
DEMO_NOTICE_LINES = (
    "=" * DEMO_NOTICE_WIDTH,
    " 注意：您当前进入的是【演示模式】！",
    " 我们正在使用模拟数据为您展示功能。",
    " 若要监控真实运行的 Agent，请使用以下命令：",
    ' 启动哨兵.bat --agent mcode --cwd "你的项目路径" "任务指令"',
    "=" * DEMO_NOTICE_WIDTH,
)

#: 演示模式下「别关窗口」的醒目块（v3.0.1）—— 逐字固定。
#:
#: ⚠️ 为什么要有这个
#: ================
#:
#: v3.0.0 的实测反馈：「双击 exe 之后终端一闪而过，
#: 用户以为程序挂了。」—— 演示模式下浏览器要等一会儿才打开，
#: 那个「一会儿」就是用户关窗口的时机。
#:
#: 所以这里把「**别关**」写成**一整块**，
#: 并且在后面**反复**提醒 —— 单次提示用户扫一眼就过去了。
DEMO_KEEP_OPEN_LINES = (
    "",
    "*" * DEMO_NOTICE_WIDTH,
    " ★ 不要关闭此窗口！",
    "   看板正在浏览器打开……",
    "   关掉这个窗口，看板就断了。",
    "   想监控真实 Agent？在看板右上角点【退出演示】。",
    "*" * DEMO_NOTICE_WIDTH,
)

#: 反复提醒的间隔（秒，v3.0.1）。
DEMO_REMINDER_INTERVAL = 8.0

#: 反复提醒的次数（v3.0.1）—— 8 × 8 秒 ≈ 64 秒。
DEMO_REMINDER_TIMES = 8

#: 反复提醒时打的那一行（v3.0.1）。
DEMO_REMINDER_LINE = "[SENTINEL] 不要关闭此窗口，看板正在浏览器打开……"

#: 雷达模式的「别关窗口」醒目块（v3.0.2）—— 逐字固定。
RADAR_KEEP_OPEN_LINES = (
    "",
    "*" * DEMO_NOTICE_WIDTH,
    " ★ 不要关闭此窗口！",
    "   雷达正在扫描你机器上的 Agent 会话日志……",
    "   关掉这个窗口，扫描就断了。",
    "*" * DEMO_NOTICE_WIDTH,
)

#: 雷达模式反复提醒时打的那一行（v3.0.2）。
RADAR_REMINDER_LINE = (
    "[SENTINEL] 不要关闭此窗口，雷达正在扫描 Agent 会话日志……"
)

#: 启动菜单的宽度（v3.0.2）—— 与演示说明同一个宽度，看着是一套东西。
START_MENU_WIDTH = 60

#: 无参启动时的**交互菜单**（v3.0.2）—— 逐字固定。
#:
#: ⚠️ 为什么要有这个
#: ================
#:
#: v3.0.1 的实测反馈：「**双击 exe 依然直接进演示模式**」。
#:
#: 演示模式本身没有错 —— 它该做的是「让人先看看长什么样」。
#: 错的是**双击就默认进演示**：用户以为「雷达扫不到真实的进程」，
#: 是因为哨兵**压根没问他要干什么**就替他决定了。
#:
#: 所以这里把选择权**交回给用户**：三个选项，一句话说清每个是干什么的。
START_MENU_LINES = (
    "=" * START_MENU_WIDTH,
    " 哨兵 Sentinel —— 你想让它做什么？",
    "=" * START_MENU_WIDTH,
    "",
    " [1] 启动演示模式 (体验界面)",
    "       全是模拟数据，不碰你的任何进程。",
    "",
    " [2] 开启雷达 (扫描已有的 Agent 日志)",
    "       只读你自己机器上的 Agent 会话日志 —— 推荐。",
    "",
    " [3] 接管并启动新的 Agent",
    "       边跑边审：告警当场拦下。",
    "",
    "=" * START_MENU_WIDTH,
)

#: 菜单的默认选项（v3.0.2）—— 直接回车就选它。
#:
#: ⚠️ 为什么默认是「雷达」而不是「演示」：
#: 演示数据**不是**用户的真实环境；雷达扫的是**用户自己的**日志。
#: 不问就默认给「真实的东西」，比默认给「假的」诚实。
START_MENU_DEFAULT = "2"

#: 菜单三个选项的合法取值（v3.0.2）。
START_MENU_CHOICES = ("1", "2", "3")


def print_start_menu(stream: Any = None) -> None:
    """打印启动菜单（v3.0.2）。

    编码安全的处理与 :func:`print_demo_notice` 一致 ——
    菜单写不出去**绝不能**把启动带崩。

    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    """
    out = stream if stream is not None else sys.stdout
    text = "\n".join(START_MENU_LINES) + "\n"
    try:
        out.write(text)
    except UnicodeEncodeError:
        encoding = getattr(out, "encoding", None) or "utf-8"
        try:
            out.write(
                text.encode(encoding, errors="replace").decode(
                    encoding, errors="replace"
                )
            )
        except (UnicodeEncodeError, LookupError, AttributeError):
            out.write(text.encode("ascii", errors="replace").decode("ascii"))
    except (OSError, ValueError):
        return
    flush = getattr(out, "flush", None)
    if flush is not None:
        try:
            flush()
        except (OSError, ValueError) as exc:
            _LOGGER.debug("刷新输出流失败：%s: %s", type(exc).__name__, exc)


def read_start_choice(
    default: str = START_MENU_DEFAULT,
    stream: Any = None,
    timeout: float = 1.5,
) -> str:
    """读用户选了哪个（v3.0.2）。

    ⚠️ **不是交互式终端时绝不阻塞**
    ================================

    stdin 被重定向、或者根本没有终端（CI、后台服务）时，
    挂在那儿等一个永远不来的按键，是最糟糕的体验。
    这时直接用默认值，并且**明确告诉用户**选了哪个。

    ⚠️ 但「重定向」和「没有输入」是两回事
    ====================================

    * **终端（TTY）** —— 正常等用户按键；
    * **普通文件**（``sentinel.exe < choices.txt``）—— 读到 EOF 就走，
      不会阻塞；
    * **管道**（``echo 2 | sentinel.exe``）—— 另起线程读，最多等
      ``timeout`` 秒；等不到就用默认值，**绝不挂死**。

    :param default: 默认选项（直接回车 / 非交互时用）。
    :param stream: 输入流；``None`` 时用 ``sys.stdin``。
    :param timeout: 管道输入最多等多久（秒）。
    :return: ``"1"`` / ``"2"`` / ``"3"`` 之一。
    """
    source = stream if stream is not None else sys.stdin
    try:
        interactive = bool(source) and source.isatty()
    except (AttributeError, ValueError, OSError):
        interactive = False

    if not interactive:
        raw = _read_choice_line(source, timeout)
        choice = _normalize_choice(raw)
        if choice in START_MENU_CHOICES:
            return choice
        print(
            "[SENTINEL] 非交互环境（没有终端可输入），已按默认选择 [{}]。".format(
                default
            ),
            flush=True,
        )
        return default

    try:
        raw = input("请输入 1 / 2 / 3 后回车（直接回车 = {}）：".format(default))
    except (EOFError, KeyboardInterrupt):
        print("", flush=True)
        return default
    except (OSError, ValueError) as exc:
        _LOGGER.debug("读取启动菜单失败：%s: %s", type(exc).__name__, exc)
        return default

    choice = _normalize_choice(raw)
    if choice in START_MENU_CHOICES:
        return choice
    return default


def _normalize_choice(raw: Any) -> str:
    """把用户输入整理成干净的选项文本。

    ⚠️ 要去掉 **BOM**
    ---------------
    Windows 上重定向进来的文本常常带 UTF-8 BOM（``\\ufeff``）——
    不去掉的话，``"\\ufeff1"`` 就不等于 ``"1"``，
    明明写了 ``1`` 却选了默认项。

    :param raw: 原始输入。
    :return: 去掉空白与 BOM 后的文本。
    """
    return str(raw or "").strip().lstrip("\ufeff").strip()


def _read_choice_line(source: Any, timeout: float) -> str:
    """从**非交互**输入流里读一行，绝不无限期阻塞。

    :param source: 输入流。
    :param timeout: 管道输入最多等多久（秒）。
    :return: 读到的内容；读不到就是空串。
    """
    try:
        fileno = source.fileno()
    except (AttributeError, OSError, ValueError):
        fileno = None

    if fileno is None:
        # 没有 ``fileno`` 的流（测试里的 ``StringIO`` 之类）——
        # 读它不会阻塞，直接读。
        try:
            return str(source.readline() or "")
        except (AttributeError, OSError, ValueError):
            return ""

    try:
        mode = os.fstat(fileno).st_mode
    except (OSError, ValueError):
        return ""

    if stat_module.S_ISREG(mode):
        # 普通文件：读到 EOF 就会返回，不会阻塞。
        try:
            return str(source.readline() or "")
        except (OSError, ValueError):
            return ""

    if not stat_module.S_ISFIFO(mode):
        # 字符设备（比如 NUL）、套接字 —— 当作「没有输入」。
        return ""

    # 管道：另起线程去读，最多等 timeout 秒 ——
    # 等不到就用默认值，绝不把启动挂死。
    box: Dict[str, str] = {}

    def _reader() -> None:
        try:
            box["line"] = str(source.readline() or "")
        except (OSError, ValueError):
            box["line"] = ""

    thread = threading.Thread(
        target=_reader, name="sentinel-start-menu", daemon=True
    )
    thread.start()
    thread.join(max(float(timeout), 0.0))
    return box.get("line", "")


def announce_demo_keep_open(
    *,
    interval: float = DEMO_REMINDER_INTERVAL,
    times: int = DEMO_REMINDER_TIMES,
    stream: Any = None,
) -> threading.Thread:
    """反复提醒「不要关闭此窗口」（v3.0.1）。

    在**后台线程**里跑 —— 主线程要留给 uvicorn。

    :param interval: 两次提醒之间隔多久（秒）。
    :param times: 提醒几次。
    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    :return: 已启动的后台线程。
    """
    return _announce_keep_open(
        DEMO_REMINDER_LINE, interval=interval, times=times, stream=stream
    )


def announce_radar_keep_open(
    *,
    interval: float = DEMO_REMINDER_INTERVAL,
    times: int = DEMO_REMINDER_TIMES,
    stream: Any = None,
) -> threading.Thread:
    """反复提醒「不要关闭此窗口」（雷达模式，v3.0.2）。

    :param interval: 两次提醒之间隔多久（秒）。
    :param times: 提醒几次。
    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    :return: 已启动的后台线程。
    """
    return _announce_keep_open(
        RADAR_REMINDER_LINE, interval=interval, times=times, stream=stream
    )


def _announce_keep_open(
    line: str,
    *,
    interval: float = DEMO_REMINDER_INTERVAL,
    times: int = DEMO_REMINDER_TIMES,
    stream: Any = None,
) -> threading.Thread:
    """按节奏反复打一行提醒（后台线程）。

    :param line: 每次要打的那一行。
    :param interval: 两次提醒之间隔多久（秒）。
    :param times: 提醒几次。
    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    :return: 已启动的后台线程。
    """
    out = stream if stream is not None else sys.stdout

    def _worker() -> None:
        """按节奏提醒，打完就收工。"""
        for _ in range(max(int(times), 0)):
            if not _safe_print(out, line):
                return
            time.sleep(max(float(interval), 0.5))

    thread = threading.Thread(
        target=_worker, name="sentinel-demo-reminder", daemon=True
    )
    thread.start()
    return thread


def _safe_print(out: Any, text: str) -> bool:
    """往流里写一行；写不出去就返回 ``False``。

    ⚠️ 控制台可能被重定向、可能已经关掉、可能是 GBK ——
    提醒写不出去**绝不能**把启动带崩。

    :param out: 输出流。
    :param text: 要写的内容。
    :return: 写成功返回 ``True``。
    """
    try:
        out.write(text + "\n")
        flush = getattr(out, "flush", None)
        if flush is not None:
            flush()
    except (OSError, ValueError, UnicodeEncodeError) as exc:
        _LOGGER.debug("写提醒失败：%s: %s", type(exc).__name__, exc)
        return False
    return True


def _print_block(lines: Tuple[str, ...], stream: Any = None) -> None:
    """打印一个由多行组成的**醒目块**（编码安全）。

    ⚠️ stdout 被重定向到文件时，Windows 会退回 GBK ——
    碰上编不出来的字符就 ``UnicodeEncodeError``，
    而这一段恰好在**启动流程**里，炸了就等于启动失败。

    所以两层兜底：先按流自己的编码写，真编不出来就退到「有损」写法
    （编不出的字符换成 ``?``），**绝不**让一句提示把启动带崩。

    :param lines: 块的每一行。
    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    """
    out = stream if stream is not None else sys.stdout
    text = "\n".join(lines) + "\n"
    try:
        out.write(text)
    except UnicodeEncodeError:
        encoding = getattr(out, "encoding", None) or "utf-8"
        try:
            out.write(
                text.encode(encoding, errors="replace").decode(
                    encoding, errors="replace"
                )
            )
        except (UnicodeEncodeError, LookupError, AttributeError):
            out.write(text.encode("ascii", errors="replace").decode("ascii"))
    except (OSError, ValueError):
        # 流被关掉了 —— 提示打不出来就算了，**绝不能**因此带崩启动。
        return
    flush = getattr(out, "flush", None)
    if flush is not None:
        try:
            flush()
        except (OSError, ValueError) as exc:
            _LOGGER.debug("刷新输出流失败：%s: %s", type(exc).__name__, exc)


def print_keep_open_notice(stream: Any = None) -> None:
    """打印「不要关闭此窗口」的醒目块（v3.0.1）。

    编码安全的处理与 :func:`print_demo_notice` 一致 ——
    提醒写不出去**绝不能**把启动带崩。

    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    """
    _print_block(DEMO_KEEP_OPEN_LINES, stream)


def print_radar_keep_open_notice(stream: Any = None) -> None:
    """打印雷达模式的「不要关闭此窗口」醒目块（v3.0.2）。

    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    """
    _print_block(RADAR_KEEP_OPEN_LINES, stream)


def print_demo_notice(stream: Any = None) -> None:
    """打印演示模式的醒目说明（**6 行，逐字固定**）。

    打印时机：**大字报之后**、看板真正起来之前。

    ⚠️ 编码安全
    ==========

    Windows 控制台本身支持中文，但 stdout 被**重定向到文件**时
    Python 会退回系统 ANSI 代码页（GBK）——
    碰上编不出来的字符就会 ``UnicodeEncodeError``，
    而这一段恰好在**启动流程**里，炸了就等于启动失败。

    所以这里做了两层兜底：先按流自己的编码写，
    真编不出来就退到「有损」写法（编不出的字符换成 ``?``），
    **绝不**让一句提示把启动带崩。

    :param stream: 输出流；``None`` 时用 ``sys.stdout``。
    """
    out = stream if stream is not None else sys.stdout
    text = "\n".join(DEMO_NOTICE_LINES) + "\n"
    try:
        out.write(text)
    except UnicodeEncodeError:
        encoding = getattr(out, "encoding", None) or "utf-8"
        try:
            out.write(
                text.encode(encoding, errors="replace").decode(
                    encoding, errors="replace"
                )
            )
        except (UnicodeEncodeError, LookupError, AttributeError):
            out.write(text.encode("ascii", errors="replace").decode("ascii"))
    except (OSError, ValueError):
        # 流被关掉了 —— 提示打不出来就算了，**绝不能**因此带崩启动。
        return
    flush = getattr(out, "flush", None)
    if flush is not None:
        try:
            flush()
        except (OSError, ValueError) as exc:
            # 内容已经写出去了，flush 失败只是缓冲区没及时落盘。
            _LOGGER.debug(
                "刷新输出流失败：%s: %s", type(exc).__name__, exc
            )
