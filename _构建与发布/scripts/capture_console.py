"""抓一张**终端窗口**的实拍图（v3.0.2 验收用）。

为什么不用 ``capture_dashboard.py``
===================================

那个脚本走的是浏览器 DevTools 协议，只能抓网页。
终端启动菜单要证明的是「双击 exe 之后终端里真的打印了三个选项」——
必须**真的开一个控制台窗口**跑一遍，然后把那个窗口拍下来。

流程：

1. 起一个 ``cmd.exe /k`` 窗口，跑入口脚本（无参数 → 打印菜单 → 等输入）；
2. 等它把菜单画出来；
3. 用 ``GetWindowRect`` 找到窗口位置，``CopyFromScreen`` 拍下来；
4. 关掉窗口。

用法::

    python _构建与发布/scripts/capture_console.py --out "<绝对路径>.png"
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

#: 项目根目录。
ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


class _RECT(ctypes.Structure):
    """Win32 的 ``RECT`` 结构。"""

    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _capture_window(handle: int, out_path: str) -> bool:
    """把一个窗口的屏幕区域拍下来。

    ⚠️ 为什么用 PowerShell 的 ``System.Drawing`` 而不用 Pillow
    ==========================================================
    截图只是**验收取证**用的，不值得为它给项目加一个依赖 ——
    ``System.Drawing`` 是 Windows 自带的，PowerShell 一行就能调。

    :param handle: 窗口句柄。
    :param out_path: 输出 PNG 的绝对路径。
    :return: 成功就返回 ``True``。
    """
    rect = _RECT()
    if not ctypes.windll.user32.GetWindowRect(
        wintypes.HWND(handle), ctypes.byref(rect)
    ):
        print("GetWindowRect 失败。", file=sys.stderr)
        return False

    left = rect.left
    top = rect.top
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        print("窗口尺寸异常：{}x{}".format(width, height), file=sys.stderr)
        return False

    script = (
        "Add-Type -AssemblyName System.Drawing;"
        "$b = New-Object System.Drawing.Bitmap({w}, {h});"
        "$g = [System.Drawing.Graphics]::FromImage($b);"
        "$g.CopyFromScreen({x}, {y}, 0, 0, $b.Size);"
        "$b.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose(); $b.Dispose();"
    ).format(
        w=width,
        h=height,
        x=left,
        y=top,
        out=out_path.replace("'", "''"),
    )

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("截图失败：{}".format(result.stderr.strip()), file=sys.stderr)
        return False
    if not os.path.exists(out_path):
        print("截图没有生成。", file=sys.stderr)
        return False

    print("已保存：{}（{}x{}）".format(out_path, width, height))
    return True


def _find_window_by_title(needle: str) -> int:
    """按**标题**找窗口。

    ⚠️ 为什么不按进程 id 找
    ======================
    Windows 10/11 上 ``cmd.exe`` 跑在 **Windows Terminal** 里 ——
    ``cmd.exe`` 自己只有一个 0x0 的 ``PseudoConsoleWindow``，
    真正看得见的窗口属于终端宿主（``CASCADIA_HOSTING_WINDOW_CLASS``），
    进程 id 根本对不上。

    所以给控制台起一个**唯一标题**，再按标题找 ——
    经典 conhost 和 Windows Terminal 都能对上。

    :param needle: 标题里要出现的片段。
    :return: 窗口句柄；找不到就是 0。
    """
    user32 = ctypes.windll.user32
    found = {"handle": 0}

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _callback(hwnd, _lparam):
        """枚举回调。"""
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if needle in title.value:
            found["handle"] = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return int(found["handle"] or 0)


def _focus_window(handle: int) -> None:
    """把窗口提到最前面 —— 不然截到的是别的窗口。

    ⚠️ 为什么要**最大化**
    ===================
    Windows 会拦「抢焦点」，``SetForegroundWindow`` 常常不生效 ——
    结果截到的是压在上面的别的窗口。最大化既能让窗口跑到前面，
    又保证菜单那一段完整地落在画面里。

    :param handle: 窗口句柄。
    """
    user32 = ctypes.windll.user32
    try:
        user32.ShowWindow(handle, 3)  # SW_MAXIMIZE
        user32.SetForegroundWindow(handle)
        user32.BringWindowToTop(handle)
    except (AttributeError, OSError):
        return
    time.sleep(1.2)


def main() -> int:
    """命令行入口。

    :return: 进程退出码。
    """
    parser = argparse.ArgumentParser(description="抓终端窗口实拍图")
    parser.add_argument("--out", required=True, help="输出 PNG 的绝对路径")
    parser.add_argument(
        "--wait", type=float, default=6.0, help="等菜单画出来的秒数"
    )
    parser.add_argument(
        "--cmd",
        default="",
        help="要在窗口里跑的命令（默认跑哨兵入口脚本）",
    )
    parser.add_argument(
        "--marker",
        default="SentinelMenuCapture",
        help="给控制台起的唯一标题（用来定位窗口）",
    )
    args = parser.parse_args()

    target = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(target), exist_ok=True)

    # ⚠️ 命令里**不要加引号**
    # ----------------------
    # ``subprocess`` 在 Windows 上会按 ``list2cmdline`` 的规则给参数加引号，
    # 加了引号再被 ``cmd.exe`` 解析一遍，路径就被拆得七零八落
    # （实测报 ``can't open file``）。项目路径里没有空格，用相对路径最省事。
    command = args.cmd or "python _构建与发布\\scripts\\sentinel_entry.py"

    # ``/k`` = 执行完命令后**不关窗口** —— 菜单正等着用户按键。
    # ``title`` = 给窗口起个唯一名字，方便按标题找到它。
    proc = subprocess.Popen(
        [
            "cmd.exe",
            "/k",
            "title {} && {}".format(args.marker, command),
        ],
        cwd=ROOT,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )

    try:
        time.sleep(args.wait)
        handle = _find_window_by_title(args.marker)
        if not handle:
            print("找不到标题含「{}」的窗口。".format(args.marker), file=sys.stderr)
            return 1
        _focus_window(handle)
        ok = _capture_window(handle, target)
        return 0 if ok else 1
    finally:
        _kill_tree(proc.pid)


def _find_console_window(pid: int) -> int:
    """按进程 id 找它的控制台窗口句柄。

    :param pid: 进程 id。
    :return: 窗口句柄；找不到就是 0。
    """
    user32 = ctypes.windll.user32
    found = {"handle": 0}

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _callback(hwnd, _lparam):
        """枚举回调。"""
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            found["handle"] = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    if found["handle"]:
        return int(found["handle"])

    # 退一步：找该进程附加的控制台（``kernel32`` 而不是 ``user32``）。
    handle = ctypes.windll.kernel32.GetConsoleWindow()
    return int(handle or 0)


def _kill_tree(pid: int) -> None:
    """把整棵进程树干掉（别留一个黑窗口在桌面上）。

    :param pid: 根进程 id。
    """
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        check=False,
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
