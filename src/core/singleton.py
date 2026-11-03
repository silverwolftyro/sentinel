"""哨兵的**进程级强制单例锁**（v5.0.1）。

这一版把它从 :mod:`src.gui.single_instance` 里搬出来，成为
**内核层**的能力 —— v5.0.0 之后哨兵是**无头守护进程**，
再让核心逻辑依赖一个 GUI 包是本末倒置。

为什么必须是「进程级」而不是「端口级」
====================================

* 端口是**可配**的（``--port 8001``）—— 换个端口就能再开一个；
* 端口探测是 **TOCTOU**（先探测、后绑定，中间有缝），
  两个实例同时启动时可能都探测通过。

命名互斥体跟端口无关，**一个会话只允许一个哨兵**。

关于 PyInstaller 单文件的父子进程
================================

``--onefile`` 模式下，exe 双击后的进程结构是::

    父进程（bootloader）          子进程（真正的 Python）
    ├─ 解压到 %TEMP%\\_MEIxxxxxx  ├─ 跑入口脚本
    └─ 等子进程退出，然后自己退出   └─ **只有它会跑 Python 代码**

⚠️ **引导器（父进程）根本不执行 Python 代码**，
   所以它**永远不会**碰到本模块 —— 单例锁天然就落在
   **真正的核心执行进程**上。

任务管理器里因此会出现**两个同名进程** —— 那是
「一个引导器 + 一个核心进程」，**是一个实例，不是双开**。

即便如此，本模块仍然把这件事**显式判定并记录下来**
（见 :func:`core_process_info`），而不是靠假设：

* 用 ``PYINSTALLER_RESET_ENVIRONMENT`` 判断自己是不是单文件子进程
  （父进程给子进程打这个标记，自己没有）；
* 用 Toolhelp32 快照比对**父进程的可执行文件名**与自己的，
  看父进程是不是那个同名引导器；
* 预留 ``SENTINEL_SINGLE_INSTANCE_ROLE=bootstrap|core``
  环境变量 —— 万一将来某个打包方式真的在父进程里跑 Python，
  **不用改代码**就能把锁只绑在核心进程上。

其它平台（Linux / macOS）退化成 **文件锁**（``fcntl.flock``）——
同样不需要清理：进程退出时内核自动释放。

本模块**只读标准库**，不引入任何新依赖，也不 import 任何
``src.*`` 子模块 —— 这是它能被放在**入口最早期**的前提。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ALREADY_RUNNING_EXIT_CODE",
    "LEGACY_SINGLE_INSTANCE_NAMES",
    "SINGLE_INSTANCE_NAME",
    "SingleInstance",
    "acquire_single_instance",
    "core_process_info",
    "is_core_process",
    "release_single_instance",
    "report_already_running",
]


#: 命名互斥体的名字（v5.0.1 起统一在这里定义）。
#:
#: ⚠️ **不带 ``Global\\`` 前缀** —— 全局命名空间需要
#: ``SeCreateGlobalPrivilege`` 特权，普通用户建不出来。
#: 不带前缀的互斥体落在**当前会话**的命名空间里，
#: 对「一个会话只允许一个哨兵」这个需求**完全够用**。
#:
#: ⚠️ 源码模式与打包模式**必须用同一个名字** ——
#:    否则两个模式能同时跑。
SINGLE_INSTANCE_NAME = "SentinelSingleInstance"

#: v5.0.1 之前的互斥体名字。
#:
#: ⚠️ 保留它是为了**平滑升级**：老版本哨兵还在跑时，
#:    新版本不应该再起一个 —— 否则两个实例会抢同一个端口。
#:    探测到旧名字存在时，同样按「已经在跑」处理。
LEGACY_SINGLE_INSTANCE_NAMES = ("SentinelDesktopSingleInstance",)

#: 「已经有实例在跑」时的退出码。
#:
#: ⚠️ v5.0.1 改成 **0**：对无头守护进程来说，
#:    「已经有实例在跑」是一次**成功的空操作**，
#:    不是启动失败 —— 用非 0 退出码会让看门狗/脚本误判成故障。
#:    需要区分「正常退出」与「已经在跑」的调用方，
#:    请看终端提示文本（见 :func:`report_already_running`）。
ALREADY_RUNNING_EXIT_CODE = 0

#: Windows 的 ``ERROR_ALREADY_EXISTS``。
_ERROR_ALREADY_EXISTS = 183

#: 进程内**唯一**的单例锁 —— 拿过一次就不再重复拿。
_SINGLETON: Optional["SingleInstance"] = None


# ---------------------------------------------------------------------------
# 核心进程判定
# ---------------------------------------------------------------------------
class _PROCESSENTRY32W(ctypes.Structure):
    """``PROCESSENTRY32W`` —— 进程快照里的一条记录。"""

    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _parent_image_name() -> str:
    """取**父进程**的可执行文件名。

    :return: 父进程的 exe 文件名；取不到就返回空串。
    """
    if os.name != "nt":
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot
        snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        snapshot.restype = ctypes.c_void_p
        first = kernel32.Process32FirstW
        first.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)
        ]
        first.restype = ctypes.c_int
        nxt = kernel32.Process32NextW
        nxt.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)
        ]
        nxt.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        logger.debug("读进程快照失败：%s: %s", type(exc).__name__, exc)
        return ""

    # TH32CS_SNAPPROCESS
    handle = snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return ""
    parent_pid = os.getppid()
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        ok = first(handle, ctypes.byref(entry))
        while ok:
            if int(entry.th32ProcessID) == int(parent_pid):
                return str(entry.szExeFile)
            ok = nxt(handle, ctypes.byref(entry))
    except (OSError, ValueError) as exc:
        logger.debug("遍历进程快照失败：%s: %s", type(exc).__name__, exc)
    finally:
        try:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        except (AttributeError, OSError):
            pass
    return ""


def core_process_info() -> Dict[str, Any]:
    """判定当前进程在打包产物里扮演什么角色。

    ⚠️ **不要凭猜测** —— 这个函数把「我是不是核心进程」
       变成**可观测的事实**，而不是假设。

    :return: 形如 ``{"role": "core", "frozen": bool,
        "onefile_child": bool, "parent_image": str,
        "parent_is_bootstrap": bool, "detail": str}`` 的字典。
    """
    frozen = bool(getattr(sys, "frozen", False))
    parent_image = _parent_image_name() if frozen else ""
    own_image = (
        os.path.basename(sys.executable) if frozen else ""
    )
    parent_is_bootstrap = bool(
        parent_image
        and own_image
        and parent_image.lower() == own_image.lower()
    )
    onefile_child = (
        str(os.environ.get("PYINSTALLER_RESET_ENVIRONMENT", "")).strip()
        == "1"
    )
    override = str(
        os.environ.get("SENTINEL_SINGLE_INSTANCE_ROLE", "")
    ).strip().lower()

    if override in ("core", "bootstrap"):
        role = override
        detail = "由 SENTINEL_SINGLE_INSTANCE_ROLE 指定"
    elif not frozen:
        role = "core"
        detail = "源码模式 —— 只有这一个进程"
    elif onefile_child:
        role = "core"
        detail = (
            "PyInstaller 单文件子进程（父进程给子进程打了 "
            "PYINSTALLER_RESET_ENVIRONMENT 标记）"
        )
    elif parent_is_bootstrap:
        role = "core"
        detail = "父进程是同名引导器 —— 我们是核心子进程"
    else:
        role = "core"
        detail = "打包模式 —— 直接启动的核心进程"

    return {
        "role": role,
        "frozen": frozen,
        "onefile_child": onefile_child,
        "parent_image": parent_image,
        "parent_is_bootstrap": parent_is_bootstrap,
        "detail": detail,
    }


def is_core_process() -> bool:
    """当前进程是不是**真正干活的**那个。

    :return: 是核心进程返回 ``True``。
    """
    return core_process_info()["role"] == "core"


# ---------------------------------------------------------------------------
# 单例锁
# ---------------------------------------------------------------------------
class SingleInstance:
    """单例锁（Windows 命名互斥体 / 其它平台文件锁）。

    :param name: 互斥体名字。
    :param legacy_names: 额外要探测的**旧版**名字。
    """

    def __init__(
        self,
        name: str = SINGLE_INSTANCE_NAME,
        legacy_names: tuple = LEGACY_SINGLE_INSTANCE_NAMES,
    ) -> None:
        self.name = str(name or SINGLE_INSTANCE_NAME)
        self.legacy_names = tuple(legacy_names or ())
        #: 生效的机制：``mutex`` / ``file`` / ``unavailable``。
        self.mode = ""
        #: 是不是真的拿到了锁。
        self.acquired = False
        self._handle: Optional[int] = None
        self._lock_file = None

    # -- 对外 -------------------------------------------------------------
    def acquire(self) -> bool:
        """尝试拿到锁。

        :return: 拿到（或者**无法判断**因而放行）返回 ``True``；
            **别的进程已经在跑**返回 ``False``。
        """
        if os.name == "nt":
            return self._acquire_mutex()
        return self._acquire_file_lock()

    def release(self) -> None:
        """释放锁（进程退出时**不需要**调用 —— 内核会自动收）。"""
        if self._handle is not None:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_int
                kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            except (AttributeError, OSError) as exc:
                logger.debug("关闭互斥体失败：%s: %s", type(exc).__name__, exc)
            self._handle = None
        if self._lock_file is not None:
            try:
                self._lock_file.close()
            except OSError as exc:
                logger.debug("关闭锁文件失败：%s: %s", type(exc).__name__, exc)
            self._lock_file = None
        self.acquired = False

    # -- 内部：Windows ----------------------------------------------------
    def _acquire_mutex(self) -> bool:
        """用命名互斥体做单例（Windows）。

        :return: 拿到锁返回 ``True``；已被别人占用返回 ``False``。
        """
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create.restype = ctypes.c_void_p
        handle = create(None, False, self.name)
        if not handle:
            # 建不出来 —— **无法判断**是不是已经有实例在跑。
            # 这时候**放行**（宁可双开，也别让哨兵永远起不来）。
            logger.warning(
                "创建单例互斥体失败（Windows 错误 %s）—— 本次不做单例拦截。",
                ctypes.get_last_error(),
            )
            self.mode = "unavailable"
            self.acquired = True
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            # 已经有实例了 —— 句柄是从**别人**那儿拿到的引用，得还回去。
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            self.mode = "mutex"
            self.acquired = False
            logger.info("单例互斥体已被占用：%s", self.name)
            return False
        self._handle = int(handle)
        self.mode = "mutex"
        self.acquired = True
        # 顺手看一眼旧版名字 —— 老哨兵还在跑的话也别再起一个。
        for legacy in self.legacy_names:
            if self._named_object_exists(legacy):
                logger.info("检测到旧版哨兵仍在运行：%s", legacy)
                self.release()
                self.mode = "mutex"
                self.acquired = False
                return False
        logger.info("已取得单例互斥体：%s", self.name)
        return True

    def _named_object_exists(self, name: str) -> bool:
        """某个命名对象是不是已经存在。

        :param name: 互斥体名字。
        :return: 存在返回 ``True``。
        """
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateMutexW
            create.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p
            ]
            create.restype = ctypes.c_void_p
            handle = create(None, False, str(name))
            if not handle:
                return False
            exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return exists
        except (AttributeError, OSError) as exc:
            logger.debug("探测命名对象失败：%s: %s", type(exc).__name__, exc)
            return False

    # -- 内部：其它平台 ---------------------------------------------------
    def _acquire_file_lock(self) -> bool:
        """用文件锁做单例（Linux / macOS）。

        :return: 拿到锁返回 ``True``；已被别人占用返回 ``False``。
        """
        try:
            import fcntl
        except ImportError:
            logger.warning("本平台没有 fcntl —— 本次不做单例拦截。")
            self.mode = "unavailable"
            self.acquired = True
            return True
        path = os.path.join(
            tempfile.gettempdir(), "sentinel-single-instance.lock"
        )
        try:
            # ⚠️ 故意**不**用 with —— 锁文件要**一直**开着，
            #    进程退出时内核才自动释放 flock。
            handle = open(path, "a+b")  # noqa: SIM115 —— 锁文件要活到进程结束
        except OSError as exc:
            logger.warning(
                "打开单例锁文件失败（%s: %s）—— 本次不做单例拦截。",
                type(exc).__name__,
                exc,
            )
            self.mode = "unavailable"
            self.acquired = True
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.mode = "file"
            self.acquired = False
            return False
        self._lock_file = handle
        self.mode = "file"
        self.acquired = True
        logger.info("已取得单例文件锁：%s", path)
        return True


def acquire_single_instance(
    name: str = SINGLE_INSTANCE_NAME,
) -> Optional[SingleInstance]:
    """拿到单例锁。

    ⚠️ **进程内幂等** —— 同一进程重复调用拿到的是**同一个**对象。
    这一点很关键：入口会先后在「最早期引导」与「正式入口」两处
    上锁，但它们在**同一个进程**里 —— 不能自己把自己拦下来。

    :param name: 互斥体名字。
    :return: 拿到锁返回对象；**别的进程已经在跑**返回 ``None``。
    """
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    guard = SingleInstance(name)
    if not guard.acquire():
        return None
    _SINGLETON = guard
    return guard


def release_single_instance() -> None:
    """释放单例锁（测试用；正常退出不需要调用）。"""
    global _SINGLETON
    if _SINGLETON is not None:
        _SINGLETON.release()
    _SINGLETON = None


# ---------------------------------------------------------------------------
# 提示
# ---------------------------------------------------------------------------
def already_running_message() -> str:
    """「哨兵已在运行中」这句提示。

    ⚠️ v5.0.1：**不再提「系统托盘」** —— v5.0.0 已经把前端
       整体剥离，哨兵是**无头守护进程**，压根没有托盘图标。
       照旧文案说会让用户去找一个不存在的东西。

    :return: 提示正文。
    """
    return (
        "哨兵已在运行中。\n"
        "\n"
        "不会启动第二个实例 —— 两个哨兵会互相抢端口、抢同一份日志。\n"
        "\n"
        "请先彻底退出已经在跑的那一个（关掉它的命令行窗口，\n"
        "或用任务管理器结束它的进程），再重新启动。"
    )


def report_already_running(stream: Optional[object] = None) -> None:
    """把「哨兵已在运行中」这条提示**写到终端**。

    ⚠️ v5.0.1：**不再弹模态框**。无头守护进程在后台运行时
       弹窗毫无意义 —— 而且那个框会**默认挂 10 秒**
       （``MessageBoxTimeoutW`` 的超时），让「第二个实例」
       在任务管理器里**赖着不走**，看起来就像双开。
       这里改成**打印完立刻退出**。

    :param stream: 输出流；``None`` 用标准输出。
    """
    out = stream if stream is not None else sys.stdout
    lines = [
        "[SENTINEL] 哨兵已在运行中 —— 不会启动第二个实例。",
        "           请先彻底退出已经在跑的那一个（关掉它的命令行窗口，",
        "           或用任务管理器结束它的进程），再重新启动。",
    ]
    for line in lines:
        try:
            print(line, file=out, flush=True)
        except (OSError, ValueError, UnicodeEncodeError) as exc:
            logger.info("提示输出失败：%s: %s", type(exc).__name__, exc)
