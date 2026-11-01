"""v3.8.0 演示公用：**真实控制台窗口**与**真实屏幕截图**工具。

两个演示脚本（单例锁 / L2 掉线热切）都需要「把进程开在一个看得见的
控制台里，然后把那块屏幕拍下来」。这部分机制完全一样，
所以抽到这里共用。

⚠️ 三个必须记住的坑（都是本机实测踩出来的）：

1. ``CREATE_NEW_CONSOLE`` 拉起来的进程，标准句柄**默认继承父进程**
   （父进程是管道 → 管道），子进程打印的东西全跑进了管道，
   新开的窗口**一片漆黑**。
   → 解决办法是让子进程自己 ``CreateFileW("CONOUT$")``，
   再 ``dup2`` 到 fd 1 / fd 2（见 :func:`attach_console`）。

2. 子进程最后要靠 ``sys.stdin.readline()`` 撑着窗口不关；
   ``stdin`` 一旦给 ``DEVNULL``（读到 EOF），它会立刻退出、窗口消失。
   → 所以 stdin 给 ``PIPE``。

3. Windows Terminal（ConPTY）下，**真正可见的那个窗口归
   WindowsTerminal.exe 所有**，而不是归被拉起的客户端进程所有；
   按 PID 只能找到 ConPTY 的隐藏辅助窗口。
   → 所以定位窗口要靠**标题**（见 :func:`find_visible_window`）。

用法::

    from console_bridge import attach_console, capture_screen

    attach_console()          # 把输出接到当前那块控制台
    print("这段就会出现在窗口里")
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import List, Optional, Tuple

#: Windows 的 ``CREATE_NEW_CONSOLE``。
CREATE_NEW_CONSOLE = 0x00000010

#: 本机屏幕逻辑分辨率（2560x1600 @150% DPI ⇒ 1707x1067）。
SCREEN_W = 1707
SCREEN_H = 1067


def attach_console() -> bool:
    """把当前进程的 stdout / stderr 接到**自己所在的那块控制台**上。

    ⚠️ 这是「截到真实终端输出」的关键，见模块文档第 1 条坑。

    :return: 接入成功返回 ``True``。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            "CONOUT$",
            0x40000000 | 0x80000000,  # GENERIC_WRITE | GENERIC_READ
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None, 3,  # OPEN_EXISTING
            0, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if not handle or handle == invalid:
            return False
        import msvcrt

        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        return True
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False


def prepare_console(title: str) -> None:
    """把控制台调成 UTF-8 代码页并设好窗口标题。

    :param title: 窗口标题。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleOutputCP.argtypes = [ctypes.c_uint32]
        kernel32.SetConsoleOutputCP.restype = ctypes.c_bool
        kernel32.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
        kernel32.SetConsoleTitleW.restype = ctypes.c_bool
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleTitleW(title)
    except (AttributeError, OSError, TypeError):
        pass


def make_utf8_stdout() -> None:
    """把标准输出/错误改成 UTF-8 且**行缓冲**。

    ⚠️ 行缓冲很关键：接上控制台之后 Python 仍认为 stdout 不是 TTY，
    默认是块缓冲 —— 不改的话文字要等缓冲区满了才刷出来。

    :return: 无。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (ValueError, OSError):
            pass


def capture_screen(
    path: str, region: Optional[Tuple[int, int, int, int]] = None
) -> bool:
    """真屏幕截图。

    用 PowerShell 的 ``CopyFromScreen`` —— 这是**真的**抓屏幕像素，
    不是把文字画成一张图片。

    :param path:   输出路径（PNG）。
    :param region: 截取区域 ``(x, y, w, h)``；``None`` 截整屏。
    :return: 成功返回 ``True``。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if region is None:
        grab = "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        size = "$b.Width, $b.Height"
        origin = "$b.Location"
        bounds = "$b.Size"
    else:
        left, top, width, height = region
        grab = (
            "$b=New-Object System.Drawing.Rectangle "
            "{0}, {1}, {2}, {3};".format(left, top, width, height)
        )
        size = "$b.Width, $b.Height"
        origin = "$b.Location"
        bounds = "$b.Size"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms, System.Drawing;"
        + grab
        + "$bmp=New-Object System.Drawing.Bitmap {size};"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen({origin}, [System.Drawing.Point]::Empty, {bounds});"
        "$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose(); $bmp.Dispose();"
    ).format(size=size, origin=origin, bounds=bounds,
             path=path.replace("'", "''"))
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print("  [警告] 截图失败：{}: {}".format(type(exc).__name__, exc))
        return False
    if completed.returncode != 0:
        print("  [警告] 截图命令返回 {}：{}".format(
            completed.returncode, (completed.stdout or "").strip()
        ))
        return False
    return os.path.exists(path)


def enumerate_windows() -> List[Tuple[int, int, bool, str]]:
    """枚举所有顶层窗口（句柄、属主 PID、是否可见、标题）。

    :return: 窗口元组列表（顺序即 z-order，从上到下）。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        prototype = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        rows: List[Tuple[int, int, bool, str]] = []

        def _collect(hwnd, _lparam):
            owner = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(
                ctypes.c_void_p(hwnd), ctypes.byref(owner)
            )
            length = user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, length + 1)
            visible = bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
            rows.append((int(hwnd), int(owner.value), visible, buf.value))
            return True

        callback = prototype(_collect)
        user32.EnumWindows(callback, 0)
        return rows
    except (AttributeError, OSError, TypeError):
        return []


def find_visible_window(fragment: str) -> Optional[int]:
    """按标题**片段**找一个可见的顶层窗口。

    ⚠️ ConPTY 下可见窗口归 WindowsTerminal.exe 所有，
    按 PID 找不到它（见模块文档第 3 条坑）。

    :param fragment: 标题片段（区分大小写）。
    :return: 窗口句柄；找不到返回 ``None``。
    """
    for handle, _pid, visible, title in enumerate_windows():
        if visible and fragment and fragment in title:
            return handle
    return None


def find_window_by_pid(pid: int) -> Optional[int]:
    """按**拥有者进程 PID** 找顶层窗口。

    :param pid: 拥有者进程 ID。
    :return: 窗口句柄；找不到返回 ``None``。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        prototype = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        found: List[int] = []

        def _collect(hwnd, _lparam):
            owner = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(
                ctypes.c_void_p(hwnd), ctypes.byref(owner)
            )
            if owner.value == pid:
                found.append(int(hwnd))
            return True

        callback = prototype(_collect)
        user32.EnumWindows(callback, 0)
        if not found:
            return None
        for handle in found:
            if user32.IsWindowVisible(ctypes.c_void_p(handle)):
                return handle
        return found[0]
    except (AttributeError, OSError, TypeError):
        return None


def find_window(title: str) -> Optional[int]:
    """按**精确标题**找顶层窗口。

    :param title: 窗口标题。
    :return: 窗口句柄；找不到返回 ``None``。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        return int(user32.FindWindowW(None, title) or 0) or None
    except (AttributeError, OSError, TypeError):
        return None


def window_rect(handle: int) -> Optional[Tuple[int, int, int, int]]:
    """取窗口在屏幕上的矩形。

    :param handle: 窗口句柄。
    :return: ``(left, top, width, height)``；取不到返回 ``None``。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        user32.GetWindowRect.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(RECT)
        ]
        user32.GetWindowRect.restype = ctypes.c_bool
        if not user32.GetWindowRect(ctypes.c_void_p(handle),
                                     ctypes.byref(rect)):
            return None
        width = int(rect.right) - int(rect.left)
        height = int(rect.bottom) - int(rect.top)
        if width <= 0 or height <= 0:
            return None
        return int(rect.left), int(rect.top), width, height
    except (AttributeError, OSError, TypeError):
        return None


def place_window(
    handle: int,
    left: int,
    top: int,
    width: int,
    height: int,
) -> bool:
    """把窗口**移到指定位置并置顶**（TOPMOST）。

    置顶是为了让别的窗口盖不住它；
    挪位置是为了让终端内容落在屏幕上**没人挡的那一块**，
    保证截出来的那张图里文字是完整的。

    :param handle: 窗口句柄。
    :param left: 左上角 x（逻辑像素）。
    :param top: 左上角 y（逻辑像素）。
    :param width: 宽（逻辑像素）。
    :param height: 高（逻辑像素）。
    :return: 成功返回 ``True``。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.ShowWindow(ctypes.c_void_p(handle), 9)  # SW_RESTORE
        return bool(user32.SetWindowPos(
            ctypes.c_void_p(handle),
            ctypes.c_void_p(-1),  # HWND_TOPMOST
            int(left), int(top), int(width), int(height),
            0x0040,  # SWP_SHOWWINDOW
        ))
    except (AttributeError, OSError, TypeError):
        return False


def minimize_handle(handle: int) -> bool:
    """把指定窗口最小化（免得挡住截图）。

    :param handle: 窗口句柄。
    :return: 成功返回 ``True``。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int
        user32.ShowWindow(ctypes.c_void_p(handle), 6)  # SW_MINIMIZE
        return True
    except (AttributeError, OSError, TypeError):
        return False


def launch_in_console(
    script_path: str,
    args: Optional[List[str]] = None,
    title: str = "SENTINEL-DEMO",
    exit_file: str = "",
    hold: bool = False,
) -> subprocess.Popen:
    """把一个 Python 脚本开在一个**看得见的**独立控制台里跑。

    脚本本身要支持 ``--console-run`` 模式：接到控制台后，
    再由它把真正要跑的东西拉起来。

    ⚠️ ``stdin`` 必须给 ``PIPE``（不能给 ``DEVNULL``）——
    子进程最后要 ``sys.stdin.readline()`` 撑着窗口不关，
    读 EOF 就等于直接退出、窗口消失（见模块文档第 2 条坑）。

    :param script_path: 被拉起脚本的绝对路径。
    :param args: 传给它的参数（``--console-run`` 由这里自动追加）。
    :param title: 控制台窗口标题（ASCII）。
    :param exit_file: 退出码落盘路径（空 = 不落盘）。
    :param hold: 是否让窗口在跑完后**留着**等截图。
    :return: 子进程对象。
    """
    merged = os.environ.copy()
    merged.update({
        "SENTINEL_DEMO_ARGS": __import__("json").dumps(list(args or [])),
        "SENTINEL_DEMO_TITLE": title,
        "SENTINEL_DEMO_EXITFILE": exit_file,
        "SENTINEL_DEMO_HOLD": "1" if hold else "0",
    })
    return subprocess.Popen(
        [sys.executable, script_path, "--console-run"],
        creationflags=CREATE_NEW_CONSOLE,
        stdin=subprocess.PIPE,
        env=merged,
    )
