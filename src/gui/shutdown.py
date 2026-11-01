"""哨兵的**彻底退出**协调器（v3.4.0）。

背景
====

v3.3.x 的退出路径有**三个洞**，用户实测撞了个正着：

1. **关掉黑框，后台还在跑** ——
   Windows 上双击关闭控制台窗口**不会**杀掉控制台程序，
   进程会变成「幽灵」：任务管理器里有一个 ``sentinel.exe``，
   但用户**找不到任何地方**能把它关掉。
2. **找不到「退出系统」的按钮** ——
   看板是网页，它没有「关闭自己」的能力；
   终端黑框不是按钮，用户不会想到去那里找。
3. **退出时不干净** ——
   哨兵自己拉起的 Agent 子进程可能变成孤儿，
   在后台继续跑，用户完全不知情。

这一版把三条路**收敛到同一个出口**
====================================

* **系统托盘**（:mod:`src.gui.tray`）——
  右键菜单里的【彻底退出】；
* **看板右上角的【关闭哨兵】按钮** ——
  点一下 → 确认 → ``POST /api/shutdown``；
* **关掉黑框** ——
  Windows 的 ``CTRL_CLOSE_EVENT`` 处理器（见
  :func:`install_console_close_handler`）。

三条路最终都调用同一个 :meth:`ShutdownCoordinator.request`，
所以**只有一套收尾逻辑**，不会出现「这条路干净、那条路留残留」。

红线
====

* ⚠️ **只结束哨兵自己拉起的子进程** ——
  雷达挂载进来的 Agent 是**用户自己**启动的，
  哨兵**绝不**去杀它（R6）。
* ⚠️ 本模块**只读标准库**，不引入任何新依赖。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from typing import Callable, List, Optional

__all__ = [
    "ShutdownCoordinator",
    "install_console_close_handler",
]

logger = logging.getLogger(__name__)

#: Windows 控制台事件（``SetConsoleCtrlHandler`` 的第二参数）。
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

#: 值得**立刻退出**的控制台事件。
#:
#: ⚠️ ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT`` **不在其中** ——
#: 它们是「用户按了 Ctrl+C」，这时候走 Python 自己的
#: ``KeyboardInterrupt`` 路径更合适，不用抢。
_TERMINAL_EVENTS = frozenset(
    {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}
)


class ShutdownCoordinator:
    """把「我要退出」这件事**收成一处**。

    协调器本身**不知道** uvicorn、托盘、子进程长什么样 ——
    它只做三件事：

    1. 保证「只退出一次」（幂等）；
    2. 通知 uvicorn 停机（``should_exit = True``）；
    3. 依次跑**收尾钩子**（停托盘、结束子进程……）。

    钩子按**注册顺序**执行，每个钩子单独 try ——
    一个钩子炸了不能挡住后面的（不然就真的留下残留了）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = False
        self._reason = ""
        self._server: Optional[object] = None
        self._hooks: List[Callable[[str], None]] = []

    # -- 绑定 -----------------------------------------------------------
    def bind_server(self, server: Optional[object]) -> None:
        """绑定 uvicorn 的 :class:`uvicorn.Server` 实例。

        :param server: 服务器对象；``None`` 表示还没绑。
        """
        with self._lock:
            self._server = server

    def add_hook(self, hook: Callable[[str], None]) -> None:
        """登记一个**收尾钩子**。

        :param hook: 形如 ``hook(reason: str) -> None`` 的可调用对象。
        """
        with self._lock:
            self._hooks.append(hook)

    # -- 查询 -----------------------------------------------------------
    @property
    def requested(self) -> bool:
        """是否已经请求过退出。"""
        with self._lock:
            return self._requested

    @property
    def reason(self) -> str:
        """退出的原因（``tray`` / ``api`` / ``console_closed`` …）。"""
        with self._lock:
            return self._reason

    # -- 触发 -----------------------------------------------------------
    def request(self, reason: str = "unknown") -> bool:
        """请求退出。

        **幂等** —— 重复调用只生效一次。
        这一点很重要：托盘、看板按钮、Ctrl+C 三条路可能同时触发，
        收尾逻辑跑两遍会出问题（比如把已经在退的进程再杀一次）。

        :param reason: 退出原因。
        :return: 这次调用是否**真的**触发了退出。
        """
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            self._reason = str(reason or "unknown")
            server = self._server
            hooks = list(self._hooks)
        logger.info("收到退出请求（原因：%s）", reason)
        # ⚠️ 先让 uvicorn 停机 —— 主循环下一轮就干净退出，
        #    不靠强杀。
        if server is not None:
            try:
                server.should_exit = True
            except Exception as exc:  # noqa: BLE001 —— 停机失败也要继续收尾
                logger.warning(
                    "通知 uvicorn 停机失败：%s: %s", type(exc).__name__, exc
                )
        # 再跑收尾钩子。
        for hook in hooks:
            try:
                hook(self.reason)
            except Exception as exc:  # noqa: BLE001 —— 一个钩子炸了不挡后面的
                logger.warning(
                    "退出钩子失败：%s: %s", type(exc).__name__, exc
                )
        return True


#: 控制台关闭处理器的**强引用**。
#:
#: ⚠️ 必须留着 —— ``ctypes`` 的回调是**裸函数指针**，
#: 包装对象一旦被垃圾回收，Windows 再回调就是访问已释放内存，
#: 后果是**直接崩溃**。
_CONSOLE_HANDLER_REF: List[object] = []


def install_console_close_handler(
    coordinator: ShutdownCoordinator,
) -> bool:
    """让「关掉黑框」也走同一条退出路径（v3.4.0）。

    Windows 上关闭控制台窗口时，系统会给进程发
    ``CTRL_CLOSE_EVENT``。**默认行为是什么都不做** ——
    进程继续在后台跑，这正是用户说的「幽灵」。

    这里装一个处理器：收到关窗事件就**立刻**请求退出，
    并返回 ``True``（告诉系统「我自己处理了」）。

    ⚠️ 系统只给进程大约 5 秒退出的时间 ——
    所以这条路径必须**快**：它只置一个标志并让 uvicorn 停机，
    真正的收尾在主线程里做。

    :param coordinator: 退出协调器。
    :return: 装上了返回 ``True``；非 Windows 或失败返回 ``False``。
    """
    if os.name != "nt":
        return False
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def _handler(event: int) -> int:
        if int(event) in _TERMINAL_EVENTS:
            logger.info("控制台被关闭（事件 %s），开始退出", event)
            coordinator.request("console_closed")
            return 1
        return 0

    try:
        callback = handler_type(_handler)
        ok = bool(
            ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, 1)
        )
    except (AttributeError, OSError, ValueError) as exc:
        logger.warning(
            "安装控制台关闭处理器失败：%s: %s", type(exc).__name__, exc
        )
        return False
    if not ok:
        logger.warning("安装控制台关闭处理器失败：系统拒绝了。")
        return False
    _CONSOLE_HANDLER_REF.append(callback)
    return True
