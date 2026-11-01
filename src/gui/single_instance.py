"""哨兵的**单例锁**与**进程树同生共死**（v3.8.0）。

背景
====

用户长期反馈两个痛点：

1. **双开** —— 双击两次 exe 出现**两个托盘图标**，
   两个进程抢同一个端口、抢同一份日志、互相覆盖配置；
2. **幽灵进程** —— 关掉黑框之后后台还留着 ``sentinel.exe``，
   以及哨兵自己拉起的 Agent 子进程变成孤儿，在后台继续跑。

这一版把两条路**都**堵上。

单例锁
======

用的是 **Windows 命名互斥体**（``CreateMutexW``）——
它是**内核对象**：进程一死，句柄自动关闭，对象随之消失，
所以**不需要**写 PID 文件、不需要清理、也不会留下「锁文件没删」这种脏状态。

⚠️ **为什么不是「绑端口」**：

* 端口是**可配**的（``--port 8001``）—— 换个端口就能再开一个，
  双开照样发生；
* 端口探测是 **TOCTOU**（先探测、后绑定，中间有缝），
  两个实例同时启动时可能都探测通过。

命名互斥体跟端口无关，**一个会话只允许一个哨兵**。

其它平台（Linux / macOS）退化成 **文件锁**（``fcntl.flock``）——
同样不需要清理：进程退出时内核自动释放。

进程树同生共死
=============

用 **Windows Job Object**（``CreateJobObjectW`` +
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``）：

* 哨兵把**自己**放进这个 Job；
* Job 里的成员会**自动继承**给子进程
  （除非子进程显式带 ``CREATE_BREAKAWAY_FROM_JOB``）；
* 哨兵进程一死 → 它的 Job 句柄被系统关闭 →
  Job 内的**所有**成员（哨兵自己 + Agent + Agent 的孙进程）**一起被回收**。

⚠️ 这跟红线 R6 不冲突：
    R6 说的是「**只**结束哨兵自己拉起的子进程」——
    雷达挂载进来的、**用户自己**启动的 Agent 根本不是哨兵的子进程，
    **从来不在这个 Job 里**，因此永远不会被误杀。

⚠️ **优雅降级**：Job Object 建不起来（老系统、权限不足、
    进程已在别的 Job 里）时**只记一条警告**，
    哨兵照常工作 —— 现有的 ``taskkill /T /F`` 兜底仍然在（红线 R5）。

本模块**只读标准库**，不引入任何新依赖。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ALREADY_RUNNING_EXIT_CODE",
    "ProcessGroup",
    "SingleInstance",
    "acquire_single_instance",
    "already_running_message",
    "install_process_group",
    "release_single_instance",
    "show_already_running_dialog",
]

#: 命名互斥体的名字。
#:
#: ⚠️ **不带 ``Global\\`` 前缀** —— 全局命名空间需要
#: ``SeCreateGlobalPrivilege`` 特权，普通用户建不出来。
#: 不带前缀的互斥体落在**当前会话**的命名空间里，
#: 对「桌面端应用只能开一个」这个需求**完全够用**。
#:
#: ⚠️ 源码模式（``启动哨兵.bat``）与打包模式（``sentinel.exe``）
#:    **必须用同一个名字** —— 否则两个模式能同时跑。
SINGLE_INSTANCE_NAME = "SentinelDesktopSingleInstance"

#: 第二个实例的退出码 —— 与「正常退出（0）」「启动失败（2）」区分开。
ALREADY_RUNNING_EXIT_CODE = 3

#: Windows 的 ``ERROR_ALREADY_EXISTS``。
_ERROR_ALREADY_EXISTS = 183

#: ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: ``JobObjectExtendedLimitInformation`` 的信息类编号。
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

#: 弹窗按钮与图标（``MB_OK`` + 警告图标 + 置顶）。
_MB_OK = 0x00000000
_MB_ICONWARNING = 0x00000030
_MB_TOPMOST = 0x00040000

#: 弹窗自动关闭时限（毫秒）——
#: 没有时限的话，一个没人点掉的模态框会把进程**永远**留在那儿，
#: 那等于又造了一个幽灵。
_DIALOG_TIMEOUT_MS = 10000


# ---------------------------------------------------------------------------
# Windows 结构体（Job Object 用）
# ---------------------------------------------------------------------------
class _JobObjectBasicLimitInformation(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION``。"""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    """``IO_COUNTERS``。"""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``。"""

    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# ---------------------------------------------------------------------------
# 单例锁
# ---------------------------------------------------------------------------
class SingleInstance:
    """单例锁（Windows 命名互斥体 / 其它平台文件锁）。

    :param name: 互斥体名字。
    """

    def __init__(self, name: str = SINGLE_INSTANCE_NAME) -> None:
        self.name = str(name or SINGLE_INSTANCE_NAME)
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
            return False
        self._handle = int(handle)
        self.mode = "mutex"
        self.acquired = True
        logger.info("已取得单例互斥体：%s", self.name)
        return True

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
            tempfile.gettempdir(), "sentinel-desktop-single-instance.lock"
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


#: 进程内**唯一**的单例锁 —— 拿过一次就不再重复拿。
_SINGLETON: Optional[SingleInstance] = None


def acquire_single_instance(
    name: str = SINGLE_INSTANCE_NAME,
) -> Optional[SingleInstance]:
    """拿到单例锁。

    ⚠️ **进程内幂等** —— 同一进程重复调用拿到的是**同一个**对象。
    这一点很关键：``sentinel_entry.py`` 会转手调用
    ``run_with_sentinel.main()``，两处都要上锁，
    但它们在**同一个进程**里 —— 不能自己把自己拦下来。

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
# 进程树同生共死
# ---------------------------------------------------------------------------
class ProcessGroup:
    """Windows Job Object —— 进程树同生共死。

    :param enabled: 实际是否生效（建不起来就是 ``False``）。
    :param reason: 没生效的原因（写进日志）。
    """

    def __init__(self) -> None:
        self.enabled = False
        self.reason = ""
        self._job: Optional[int] = None

    def install(self) -> bool:
        """建 Job 并把**当前进程**放进去。

        :return: 生效返回 ``True``；降级返回 ``False``。
        """
        if os.name != "nt":
            self.reason = "非 Windows 平台，没有 Job Object"
            return False
        if str(os.environ.get("SENTINEL_JOB_OBJECT", "1")).strip() in (
            "0",
            "false",
            "False",
            "no",
        ):
            self.reason = "环境变量 SENTINEL_JOB_OBJECT 把它关掉了"
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        create_job.restype = ctypes.c_void_p
        job = create_job(None, None)
        if not job:
            self.reason = "CreateJobObjectW 失败（Windows 错误 {}）".format(
                ctypes.get_last_error()
            )
            logger.warning("%s —— 进程树同生共死不可用。", self.reason)
            return False

        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        set_info.restype = ctypes.c_int
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not set_info(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.reason = "SetInformationJobObject 失败（Windows 错误 {}）".format(
                ctypes.get_last_error()
            )
            self._close_job(kernel32, job)
            logger.warning("%s —— 进程树同生共死不可用。", self.reason)
            return False

        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        assign.restype = ctypes.c_int
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        current = kernel32.GetCurrentProcess()
        if not assign(job, current):
            self.reason = (
                "AssignProcessToJobObject 失败（Windows 错误 {}）—— "
                "进程可能已经在别的 Job 里".format(ctypes.get_last_error())
            )
            self._close_job(kernel32, job)
            logger.warning("%s —— 进程树同生共死不可用。", self.reason)
            return False

        # ⚠️ 句柄**故意不关** —— 关掉它就等于触发 KILL_ON_JOB_CLOSE，
        #    那会把哨兵自己一起杀掉。进程退出时系统会自动关它，
        #    那正是我们要的时机：哨兵一死，Job 里的成员跟着死。
        self._job = int(job)
        self.enabled = True
        self.reason = "已启用（Job Object：KILL_ON_JOB_CLOSE）"
        logger.info("进程树同生共死：%s", self.reason)
        return True

    @staticmethod
    def _close_job(kernel32: object, job: object) -> None:
        """把没派上用场的 Job 句柄还回去。"""
        try:
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(job))
        except (AttributeError, OSError, TypeError) as exc:
            logger.debug("关闭 Job 句柄失败：%s: %s", type(exc).__name__, exc)


#: 进程内**唯一**的进程组 —— 装一次就够。
_GROUP: Optional[ProcessGroup] = None


def install_process_group() -> ProcessGroup:
    """装上 Job Object（进程内幂等）。

    :return: 进程组对象；``enabled`` 说明有没有真的生效。
    """
    global _GROUP
    if _GROUP is not None:
        return _GROUP
    group = ProcessGroup()
    group.install()
    _GROUP = group
    return group


# ---------------------------------------------------------------------------
# 「已经在运行」的提示
# ---------------------------------------------------------------------------
def already_running_message() -> str:
    """「哨兵已在运行中」这句提示。

    :return: 提示正文。
    """
    return (
        "哨兵已在运行中。\n"
        "\n"
        "不会启动第二个实例 —— 两个哨兵会互相抢端口、抢托盘图标、\n"
        "抢同一份日志。\n"
        "\n"
        "请到系统托盘（右下角）找到哨兵图标，右键 →【彻底退出】，\n"
        "再重新启动。"
    )


def show_already_running_dialog(
    message: Optional[str] = None,
    timeout_ms: int = _DIALOG_TIMEOUT_MS,
) -> bool:
    """弹一个「哨兵已在运行中」的窗口框。

    ⚠️ **带自动关闭时限** —— 模态框如果没人点，
    进程就会永远卡在那儿，那等于又造了一个幽灵。

    ⚠️ 弹不出来（不是 Windows / 没有那个 API）时**不报错**，
    终端提示已经说清楚了，弹窗只是锦上添花。

    :param message: 弹窗正文；``None`` 用默认文案。
    :param timeout_ms: 自动关闭时限（毫秒）。
    :return: 真的弹了返回 ``True``。
    """
    if os.name != "nt":
        return False
    flag = str(os.environ.get("SENTINEL_SINGLE_INSTANCE_DIALOG", "1")).strip()
    if flag in ("0", "false", "False", "no"):
        return False
    text = message if message is not None else already_running_message()
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        func = user32.MessageBoxTimeoutW
    except (AttributeError, OSError) as exc:
        logger.info("弹窗不可用：%s: %s", type(exc).__name__, exc)
        return False
    func.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_ushort,
        ctypes.c_uint32,
    ]
    func.restype = ctypes.c_int
    try:
        func(
            None,
            text,
            "哨兵 Sentinel",
            _MB_OK | _MB_ICONWARNING | _MB_TOPMOST,
            0,
            int(timeout_ms),
        )
    except (OSError, ValueError) as exc:
        logger.info("弹窗失败：%s: %s", type(exc).__name__, exc)
        return False
    return True


def report_already_running(stream: Optional[object] = None) -> None:
    """把「哨兵已在运行中」这条提示送到终端（并弹窗）。

    :param stream: 输出流；``None`` 用标准输出。
    """
    out = stream if stream is not None else sys.stdout
    lines = [
        "[SENTINEL] 哨兵已在运行中 —— 不会启动第二个实例。",
        "           请到系统托盘（右下角）右键哨兵图标 →【彻底退出】，"
        "然后再试。",
    ]
    for line in lines:
        try:
            print(line, file=out, flush=True)
        except (OSError, ValueError, UnicodeEncodeError) as exc:
            logger.info("提示输出失败：%s: %s", type(exc).__name__, exc)
    show_already_running_dialog()
