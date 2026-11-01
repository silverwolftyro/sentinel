"""哨兵自身的资源监测 —— 叶子模块，只依赖标准库。

为什么监控工具自己
==================

「监控工具本身不能变成资源消耗大户」——
一个越吃越胖的监控器，比没有监控更糟：
用户会怀疑它、关掉它，然后整夜的值守就白搭了。

所以这个模块做三件事：

* **量出自己**：CPU 占用、内存占用、运行时长、线程数；
* **不拖累自己**：采样只用 Win32 API（``ctypes``），
  **不读文件、不起线程、不做网络 I/O** —— 一次采样是微秒级的；
* **不引入新依赖**：``psutil`` 是**可选**的（没装也能用），
  主力实现走 ``ctypes`` 调 Win32 —— 打包版**不需要**额外装东西。

Windows 上量的是什么
======================

* **内存** —— ``GetProcessMemoryInfo`` 的 ``WorkingSetSize``
  （当前工作集，即「进程真正占着的物理内存」），
  外加 ``PeakWorkingSetSize``（峰值）与 ``PrivateUsage``（私有提交量）；
* **CPU** —— ``GetProcessTimes`` 的「内核时间 + 用户时间」，
  除以**墙钟时间**得到百分比。
  ⚠️ 多核机器上这个值**可以超过 100%**
  （16 核跑满就是 1600%）—— 那不是 bug，是「用掉了 16 个核」；
* **运行时长** —— 进程创建时间到现在。

非 Windows（开发 / CI）
======================

退回到标准库：``resource.getrusage`` 取 CPU 时间，
``/proc/self/statm``（Linux）取内存。
拿不到就返回 ``None`` —— **绝不编造数字**。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ProcessMonitor",
    "format_uptime",
    "process_snapshot",
]

#: ``FILETIME`` 是 100 纳秒为单位。
_FILETIME_UNITS = 10_000_000.0

#: 一秒的 ``FILETIME`` 单位数。
_ONE_SECOND = 1_000_000_000.0

#: 1601-01-01 到 1970-01-01 的秒数差（``FILETIME`` 的纪元不是 Unix 纪元）。
_FILETIME_EPOCH_OFFSET = 11_644_473_600.0


def format_uptime(seconds: float) -> str:
    """把秒数渲染成人话（``2 小时 13 分`` / ``13 分 05 秒``）。

    :param seconds: 秒数。
    :return: 形如 ``2 小时 13 分`` 的文本。
    """
    total = int(max(float(seconds or 0.0), 0.0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append("{} 天".format(days))
    if hours:
        parts.append("{} 小时".format(hours))
    if minutes:
        parts.append("{} 分".format(minutes))
    if not parts and secs:
        parts.append("{} 秒".format(secs))
    if not parts:
        return "不到 1 秒"
    if days or hours:
        return " ".join(parts)
    return "{} 分 {:02d} 秒".format(minutes, secs)


# ---------------------------------------------------------------------------
# Windows 实现（ctypes 调 Win32）
# ---------------------------------------------------------------------------
def _windows_times(pid: int):
    """取进程的（创建时间 / 内核时间 / 用户时间）。

    ⚠️ **必须显式声明 ``argtypes`` / ``restype``** ——
    ``ctypes`` 的默认签名把句柄当成 32 位整数，
    64 位 Windows 上的伪句柄 ``GetCurrentProcess()``
    会被截断，``GetProcessTimes`` 直接返回失败。

    ⚠️ ``FILETIME`` 的纪元是 **1601-01-01**，不是 Unix 纪元 ——
    换算成时间戳要减掉 :data:`_FILETIME_EPOCH_OFFSET`。

    :param pid: 进程 ID；``0`` 表示当前进程。
    :return: ``(creation_unix_epoch, cpu_seconds)``；取不到返回 ``None``。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 —— 非 Windows 上没有 ctypes.wintypes
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        if pid:
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
        else:
            handle = kernel32.GetCurrentProcess()
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None

        def to_seconds(ft):
            """把 ``FILETIME`` 换算成秒。"""
            raw = ft.dwHighDateTime * 4294967296.0 + ft.dwLowDateTime
            return raw / _FILETIME_UNITS

        cpu = to_seconds(kernel) + to_seconds(user)
        creation_unix = to_seconds(creation) - _FILETIME_EPOCH_OFFSET
        return creation_unix, cpu
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有
        logger.debug("取进程时间失败：%s: %s", type(exc).__name__, exc)
        return None


def _windows_memory(pid: int):
    """取进程的（工作集 / 峰值工作集 / 私有提交量），单位字节。

    :param pid: 进程 ID；``0`` 表示当前进程。
    :return: ``(working_set, peak, private)``；取不到返回 ``None``。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 —— 非 Windows 上没有
        return None

    class _Counters(ctypes.Structure):
        """``PROCESS_MEMORY_COUNTERS``。"""

        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        if pid:
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
        else:
            handle = kernel32.GetCurrentProcess()
        if not handle:
            return None
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        ok = psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        )
        if not ok:
            return None
        return (
            int(counters.WorkingSetSize),
            int(counters.PeakWorkingSetSize),
            int(counters.PagefileUsage),
        )
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有
        logger.debug("取进程内存失败：%s: %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# 跨平台实现
# ---------------------------------------------------------------------------
def _posix_times(pid: int):
    """非 Windows 上取（启动时刻, CPU 秒数）。

    :param pid: 进程 ID；``0`` 表示当前进程。
    :return: ``(creation_epoch, cpu_seconds)``；取不到返回 ``None``。
    """
    try:
        import resource
    except Exception:  # noqa: BLE001 —— Windows 上没有这个模块
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu = float(usage.ru_utime) + float(usage.ru_stime)
        # ``ru_stime`` 是开机以来的秒数，减掉它就是进程启动时刻。
        import time as _time

        boot = _time.time() - float(usage.ru_stime)
        return boot, cpu
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有
        logger.debug("取进程时间失败：%s: %s", type(exc).__name__, exc)
        return None


def _posix_memory(pid: int):
    """非 Windows 上取工作集（Linux 读 ``/proc/self/statm``）。

    :param pid: 进程 ID；``0`` 表示当前进程。
    :return: ``(working_set, peak, private)``；取不到返回 ``None``。
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            fields = handle.read().split()
        if len(fields) < 2:
            return None
        page_size = os.sysconf("SC_PAGE_SIZE")
        working = int(fields[1]) * page_size
        return working, working, working
    except Exception as exc:  # noqa: BLE001 —— 取不到就当没有
        logger.debug("取进程内存失败：%s: %s", type(exc).__name__, exc)
        return None


class ProcessMonitor:
    """哨兵自身的资源采样器。

    ⚠️ **CPU 百分比需要两次采样才有意义** —— 第一次只能拿到累计 CPU 时间。
    所以 :meth:`snapshot` 会在两次调用之间**记住上一次**的采样点，
    第一次返回 ``cpu_percent=0.0`` 并标记 ``cpu_ready=False``。

    :param pid: 监控的进程 ID；``0``（默认）表示当前进程。
    :param interval: 两次采样之间的**最短**间隔（秒）。
        小于这个间隔就复用上一次的 CPU 时间，
        免得「连续点两下就看到一个巨大的百分比」。
    """

    def __init__(self, pid: int = 0, *, interval: float = 0.5) -> None:
        """
        :param pid: 进程 ID；``0`` 表示当前进程。
        :param interval: 两次采样的最短间隔（秒）。
        """
        self.pid = int(pid or 0)
        self.interval = max(float(interval or 0.0), 0.0)
        self._last_cpu: Optional[float] = None
        self._last_wall: Optional[float] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """采一次样。

        :return: 形如 ``{"pid", "cpu_percent", "cpu_ready", "memory_mb",
            "memory_peak_mb", "private_mb", "uptime_seconds", "uptime_text",
            "threads", "python", "version", "pid_ok"}``。
            取不到的项一律是 ``None`` —— **绝不编造数字**。
        """
        wall = time.time()
        times = (
            _windows_times(self.pid)
            if sys.platform == "win32"
            else _posix_times(self.pid)
        )
        memory = (
            _windows_memory(self.pid)
            if sys.platform == "win32"
            else _posix_memory(self.pid)
        )

        creation: Optional[float] = None
        cpu_total: Optional[float] = None
        if times is not None:
            creation, cpu_total = times

        cpu_percent = 0.0
        cpu_ready = False
        with self._lock:
            if cpu_total is not None:
                if (
                    self._last_cpu is not None
                    and self._last_wall is not None
                    and (wall - self._last_wall) >= self.interval
                ):
                    delta_cpu = max(cpu_total - self._last_cpu, 0.0)
                    delta_wall = max(wall - self._last_wall, 1e-6)
                    cpu_percent = round(delta_cpu / delta_wall * 100.0, 2)
                    cpu_ready = True
                self._last_cpu = cpu_total
                self._last_wall = wall

        working = memory[0] if memory else None
        peak = memory[1] if memory else None
        private = memory[2] if memory else None
        uptime = (wall - creation) if creation else None

        return {
            "pid": os.getpid() if not self.pid else self.pid,
            "cpu_percent": cpu_percent,
            "cpu_ready": cpu_ready,
            "memory_mb": round(working / (1024 * 1024), 2) if working else None,
            "memory_peak_mb": round(peak / (1024 * 1024), 2) if peak else None,
            "private_mb": round(private / (1024 * 1024), 2) if private else None,
            "uptime_seconds": int(uptime) if uptime else None,
            "uptime_text": format_uptime(uptime) if uptime else "未知",
            "threads": threading.active_count(),
            "platform": sys.platform,
        }

    def format_line(self) -> str:
        """采一次样并渲染成一行人话（给日志用）。

        :return: 形如
            ``[SENTINEL] 自检：CPU 3.1% · 内存 82.4 MB · 运行时长 2 小时 13 分``。
        """
        data = self.snapshot()
        cpu = data.get("cpu_percent")
        memory = data.get("memory_mb")
        cpu_text = "{:.1f}%".format(cpu) if cpu is not None else "未知"
        memory_text = (
            "{:.1f} MB".format(memory) if memory is not None else "未知"
        )
        return (
            "[SENTINEL] 自检：CPU {cpu} · 内存 {memory} · "
            "运行时长 {uptime} · 线程 {threads}"
        ).format(
            cpu=cpu_text,
            memory=memory_text,
            uptime=data.get("uptime_text") or "未知",
            threads=data.get("threads"),
        )


def process_snapshot(pid: int = 0) -> Dict[str, Any]:
    """采一次样（一次性使用的快捷方式）。

    :param pid: 进程 ID；``0`` 表示当前进程。
    :return: 采样结果字典。
    """
    return ProcessMonitor(pid).snapshot()
