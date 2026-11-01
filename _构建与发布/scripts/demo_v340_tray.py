"""v3.4.0 验收演示 —— 系统托盘 · 彻底退出 · 无残留。

它做的事**每一步都是真的**
==========================

1. 起**真的**哨兵（默认是源码模式 ``python -m src.gui.server``，
   加 ``--exe <路径>`` 就跑**打包好的** exe）——
   右下角会出现一个托盘图标；
2. 用 Win32 找到那个托盘图标**自己的窗口**（窗口类名以
   ``SystemTrayIcon`` 结尾），把鼠标挪到**通知区**，
   给它发一个 ``WM_NOTIFY``（``lParam`` 是 ``WM_RBUTTONUP``）——
   **系统真的会弹出那个右键菜单**（不是画出来的假图）；
3. 截**整块屏幕**，再按菜单的**真实屏幕坐标**裁一块出来 ——
   画面里能同时看到菜单与右下角的托盘图标；
4. 找到弹出的菜单窗口（窗口类 ``#32768``），
   用 ``MN_GETHMENU`` 拿到它的菜单句柄，
   逐项读出文字，找到【彻底退出】那一项，
   用**真鼠标事件**点它 —— 等于用户真的点了那一项；
5. 等哨兵退出，然后**证明**：

   * 进程没了（按**命令行**逐个进程核对，不只看进程名）；
   * 端口释放了；
   * 托盘图标窗口也没了。

⚠️ 跑之前必须确认**本机没有别的哨兵实例**在跑 ——
   否则「残留计数」与「托盘窗口还在」会被污染。

用法::

    python _构建与发布/scripts/demo_v340_tray.py
    python _构建与发布/scripts/demo_v340_tray.py --exe _构建与发布/dist/sentinel.exe
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

# --- Win32 常量 -----------------------------------------------------------

#: 窗口消息。
#:
#: ⚠️ ``WM_NOTIFY`` **不是**标准的那个（0x004E）——
#: pystray 用的是它自己定义的 ``WM_USER + 11``（0x040B），
#: 发错了消息菜单根本不会弹。
WM_NOTIFY = 0x040B
WM_RBUTTONUP = 0x0205
WM_CANCELMODE = 0x001F

#: ``MN_GETHMENU`` —— 问弹出菜单窗口要它的菜单句柄。
MN_GETHMENU = 0x01E1

#: 菜单项读取方式。
MF_BYPOSITION = 0x00000400

#: 鼠标事件（真点那一下用）。
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

#: 键盘：ALT（用来拿「设置前台窗口」的资格）。
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

#: 键盘：END（跳到菜单最后一项）。
VK_END = 0x23

#: 键盘：ENTER（选中当前项）。
VK_RETURN = 0x0D

#: 弹出菜单窗口的类名（Windows 标准菜单）。
MENU_WINDOW_CLASS = "#32768"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _prepare_stdout() -> None:
    """控制台可能是 GBK —— 先切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (ValueError, OSError, AttributeError):
            pass


def _enable_dpi_awareness() -> bool:
    """让本进程**按物理像素**说话。

    为什么非做不可
    =============

    哨兵跑在 150% 缩放的屏幕上。这带来一个很坑的问题：

    * ``GetSystemMetrics`` / ``GetMenuItemRect`` / ``GetCursorPos``
      在 **DPI 未感知**的进程里返回的是**逻辑像素**（缩放过）；
    * 而 ``PIL.ImageGrab.grab()`` 截出来的是**物理像素**。

    两者差一个缩放系数（这台机器上是 1.5）——
    按逻辑坐标去裁物理截图，裁出来的就是**别的地方**。

    所以演示脚本自己先设成「按显示器感知」，
    于是坐标、截图、鼠标落点**全都是物理像素**，三边对齐。

    :return: 设成了返回 ``True``；失败返回 ``False``（坐标仍是逻辑像素）。
    """
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


def _project_root() -> str:
    """向上找到项目根目录（含 ``src/core`` 的那一层）。"""
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(current, "src", "core")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _wait_http(url: str, timeout: float = 90.0) -> bool:
    """等一个 URL 能通。

    :param url: 目标地址。
    :param timeout: 最长等多少秒。
    :return: 通了返回 ``True``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.6)
    return False


# ---------------------------------------------------------------------------
# Win32 小工具
# ---------------------------------------------------------------------------
def _enum_windows() -> List[Tuple[int, str]]:
    """列出所有**顶层**窗口（句柄 + 类名）。

    :return: ``(hwnd, class_name)`` 列表。
    """
    found: List[Tuple[int, str]] = []

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _callback(hwnd: int, _lparam: int) -> bool:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(
            wintypes.HWND(hwnd), buffer, ctypes.sizeof(buffer)
        )
        found.append((int(hwnd), buffer.value))
        return True

    user32.EnumWindows(callback_type(_callback), 0)
    return found


def _find_tray_windows() -> List[int]:
    """找 pystray 托盘图标**自己的**那些窗口。

    pystray 注册的窗口类名是 ``<图标名><id>SystemTrayIcon`` ——
    以 ``SystemTrayIcon`` 结尾。

    ⚠️ 会有**两个**（图标窗口 + 菜单宿主窗口）——
    哪个是「收消息的那个」不确定，所以调用方要逐个试。

    :return: 窗口句柄列表。
    """
    return [
        hwnd
        for hwnd, class_name in _enum_windows()
        if class_name.endswith("SystemTrayIcon")
    ]


def _find_menu_window() -> Optional[int]:
    """找**弹出来的**那个菜单窗口（类名 ``#32768``）。

    :return: 窗口句柄；没找到就是 ``None``。
    """
    for hwnd, class_name in _enum_windows():
        if class_name == MENU_WINDOW_CLASS:
            return hwnd
    return None


def _gain_foreground() -> None:
    """让本进程拿到「设置前台窗口」的资格。

    Windows 的**前台锁**：后台进程不能随便抢前台。
    托盘菜单（``TrackPopupMenuEx``）要求它的宿主窗口在前台，
    所以演示脚本必须先把那个窗口提到前台。

    ⚠️ 经典的 ALT 技巧：按一下（松开）ALT，
    系统就允许本进程设置前台窗口了。
    """
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)


def _force_foreground(hwnd: int) -> bool:
    """把某个窗口**硬**提到前台。

    ⚠️ ``AttachThreadInput`` 的意思是「把两个线程的输入队列接起来」——
    接上之后，本进程就能替那个前台线程设置前台窗口了。

    :param hwnd: 目标窗口句柄。
    :return: 成功返回 ``True``。
    """
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return False
    current = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(
        wintypes.HWND(foreground), None
    )
    if not other:
        return False
    attached = user32.AttachThreadInput(
        wintypes.DWORD(current), wintypes.DWORD(other), True
    )
    try:
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))
    finally:
        if attached:
            user32.AttachThreadInput(
                wintypes.DWORD(current), wintypes.DWORD(other), False
            )


def _show_tray_menu(
    tray_windows: List[int],
    expect: str = "彻底退出",
) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    """把托盘图标的右键菜单**真的**唤出来。

    ⚠️ 这一步**有点挑环境** —— 前台锁、时序都会影响它，
    所以要多试几次：换窗口、换提前台的方式、重新发消息。

    ⚠️ 找到 ``#32768`` 菜单窗口**不等于**找到了**咱们的**菜单 ——
    桌面上别的应用（浏览器、编辑器）弹出菜单时，
    那也是 ``#32768``。所以这里**必须**读出菜单项、
    确认里面有【彻底退出】，才算数；不然就撤掉重试。

    :param tray_windows: 候选窗口句柄。
    :param expect: 菜单里必须出现的文字。
    :return: ``(菜单窗口句柄, 菜单项列表)``；没弹出来就是 ``(None, [])``。
    """
    for _attempt in range(3):
        _gain_foreground()
        for hwnd in tray_windows:
            _force_foreground(hwnd)
            time.sleep(0.4)
            # ⚠️ 托盘图标的右键事件走 ``WM_NOTIFY``（``lParam`` 是鼠标消息）——
            #    这**不是**普通的 ``WM_RBUTTONUP``，
            #    发错了消息菜单根本不会弹。
            user32.PostMessageW(
                wintypes.HWND(hwnd), WM_NOTIFY, 0, WM_RBUTTONUP
            )
            deadline = time.time() + 4
            while time.time() < deadline:
                menu_hwnd = _find_menu_window()
                if menu_hwnd:
                    items = _menu_items(menu_hwnd)
                    if any(
                        expect in str(item.get("text") or "")
                        for item in items
                    ):
                        return menu_hwnd, items
                time.sleep(0.3)
            # 上一轮可能留下了弹窗残留 —— 撤掉再试。
            user32.PostMessageW(
                wintypes.HWND(hwnd), WM_CANCELMODE, 0, 0
            )
    return None, []


def _menu_items(hwnd: int) -> List[Dict[str, Any]]:
    """读出一个弹出菜单的**每一项**（文字 + 编号 + 屏幕坐标）。

    :param hwnd: 菜单窗口句柄。
    :return: ``[{"text": …, "id": …, "rect": (l, t, r, b)}]`` 列表。
    """
    hmenu = user32.SendMessageW(
        wintypes.HWND(hwnd), MN_GETHMENU, 0, 0
    )
    if not hmenu:
        return []
    count = int(user32.GetMenuItemCount(wintypes.HMENU(hmenu)))
    items: List[Dict[str, Any]] = []
    for index in range(count):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetMenuStringW(
            wintypes.HMENU(hmenu),
            wintypes.UINT(index),
            buffer,
            ctypes.sizeof(buffer),
            wintypes.UINT(MF_BYPOSITION),
        )
        item_id = int(
            user32.GetMenuItemID(wintypes.HMENU(hmenu), wintypes.UINT(index))
        )
        rect = wintypes.RECT()
        user32.GetMenuItemRect(
            wintypes.HWND(0),
            wintypes.HMENU(hmenu),
            wintypes.UINT(index),
            ctypes.byref(rect),
        )
        items.append({
            "text": buffer.value,
            "id": item_id,
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
        })
    return items


def _dpi_scale() -> float:
    """屏幕的缩放系数（物理像素 ÷ 逻辑像素）。

    ⚠️ 为什么需要它
    ==============

    菜单是**哨兵进程**创建的，而哨兵是 **DPI 未感知**的进程 ——
    于是 ``GetMenuItemRect`` 返回的坐标是**逻辑像素**（缩放过），
    而 ``PIL.ImageGrab`` 截出来的是**物理像素**。

    两者差一个系数（这台机器 150% 缩放 = 1.5）。
    不换算就会点空 —— 菜单明明弹出来了，却点不到那一项。

    :return: 缩放系数；取不到就是 ``1.0``。
    """
    try:
        dpi = int(user32.GetDpiForSystem())
    except (AttributeError, OSError, ValueError):
        dpi = 96
    if dpi <= 0:
        dpi = 96
    return dpi / 96.0


def _hit_window(x: int, y: int) -> int:
    """某个屏幕坐标上压着哪个窗口。

    :param x: 屏幕坐标（物理像素）。
    :param y: 屏幕坐标（物理像素）。
    :return: 窗口句柄；那儿没窗口就是 ``0``。
    """
    point = wintypes.POINT(int(x), int(y))
    return int(user32.WindowFromPoint(point))


def _click_menu_item(
    item: Dict[str, Any],
    menu_hwnd: int = 0,
) -> bool:
    """在弹出菜单上**真点**一下那一项。

    ⚠️ 为什么要用真鼠标事件
    ======================

    pystray 的菜单是 ``TrackPopupMenuEx(..., TPM_RETURNCMD)`` ——
    它**不**走 ``WM_COMMAND``，而是等菜单自己返回被选中的编号。
    所以只能用鼠标事件点它 —— 这也**更真实**：
    走的正是用户鼠标点的那条路径。

    ⚠️ 点之前先**验**一下坐标落在哪儿
    --------------------------------

    菜单是哨兵进程创建的（DPI 未感知），
    它给的坐标是逻辑像素；截图与鼠标是物理像素。
    所以这里把「逻辑坐标」与「乘过缩放系数的坐标」都试一遍，
    用 ``WindowFromPoint`` 确认**真的压在菜单窗口上**才点 ——
    不然就是点空，菜单会「啪」地关掉，什么都没发生。

    :param item: :func:`_menu_items` 返回的那一项。
    :param menu_hwnd: 菜单窗口句柄；用来校验落点。
    :return: 点下去了返回 ``True``。
    """
    left, top, right, bottom = item.get("rect") or (0, 0, 0, 0)
    if right <= left or bottom <= top:
        print(
            "      菜单项坐标无效（{}）—— 放弃这一项。".format(
                item.get("rect")
            )
        )
        return False
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    scale = _dpi_scale()

    candidates = [(cx, cy)]
    scaled = (int(cx * scale), int(cy * scale))
    if scaled != candidates[0]:
        candidates.append(scaled)

    for x, y in candidates:
        if menu_hwnd:
            hit = _hit_window(x, y)
            if hit != menu_hwnd:
                print(
                    "      落点({},{}) 不在菜单上（压着的是 {}）"
                    "—— 换一组坐标。".format(x, y, hit)
                )
                continue
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.25)
        user32.mouse_event(
            MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0
        )
        user32.mouse_event(
            MOUSEEVENTF_LEFTUP, 0, 0, 0, 0
        )
        print("      已点击 ({},{})".format(x, y))
        return True
    return False


def _press_menu_key(vk: int) -> None:
    """给当前前台窗口按一个键（菜单还在的时候用）。

    :param vk: 虚拟键码。
    """
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _move_cursor_to_tray() -> Tuple[int, int]:
    """把鼠标挪到**通知区**（屏幕右下角）。

    菜单是在鼠标位置弹出来的（``TPM_RIGHTALIGN | TPM_BOTTOMALIGN``，
    所以菜单出现在鼠标的**左上方**）——
    鼠标得先在通知区，菜单才会贴着托盘图标弹出来。

    :return: 鼠标落点 ``(x, y)``。
    """
    width = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN
    height = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN
    # 停在通知区里、时钟的左边一点 ——
    # 菜单会从这里往左上弹，正好在托盘图标上方。
    x = max(0, width - 130)
    y = max(0, height - 40)
    user32.SetCursorPos(int(x), int(y))
    return x, y


def _grab_screen(
    path: str,
    crop_path: str = "",
    menu_rect: Optional[Tuple[int, int, int, int]] = None,
) -> bool:
    """截**整块屏幕**，并按菜单的**真实位置**裁一块出来。

    ⚠️ 弹出的菜单是**原生 Win32 菜单** ——
    只有真截图才能证明它真的弹出来了。

    ⚠️ 裁剪框是**算出来的**，不是写死的右下角 ——
    菜单弹在哪取决于鼠标在哪，写死会裁到别的地方去。

    :param path: 整屏截图路径（PNG）。
    :param crop_path: 裁剪图路径；留空就不裁。
    :param menu_rect: 菜单在屏幕上的坐标 ``(l, t, r, b)``。
    :return: 截到了返回 ``True``。
    """
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab()
        image.save(path)
        if crop_path:
            width, height = image.size
            if menu_rect:
                left, top, right, bottom = menu_rect
                # 往左下扩：把托盘图标与时钟一起收进来 ——
                # 「菜单是从托盘图标那儿弹出来的」这件事才看得出来。
                box = (
                    max(0, left - 40),
                    max(0, top - 40),
                    min(width, max(right + 40, width)),
                    min(height, max(bottom + 40, height)),
                )
            else:
                box = (
                    max(0, width - 900),
                    max(0, height - 620),
                    width,
                    height,
                )
            image.crop(box).save(crop_path)
            print("      裁剪框：{}".format(box))
            print("      裁剪图 → {}".format(crop_path))
        return True
    except Exception as exc:  # noqa: BLE001 —— 截不到就如实说
        print("      截图失败：{}: {}".format(type(exc).__name__, exc))
        return False


def _count_sentinel_processes(markers: List[str]) -> int:
    """数一数本机还有几个**哨兵**进程。

    ⚠️ 为什么要按**命令行**找
    ========================

    源码模式下哨兵是 ``python.exe``，打包模式才是 ``sentinel.exe`` ——
    ``tasklist /FI "IMAGENAME eq sentinel.exe"`` 在源码模式下**必然是 0**，
    那个「0」什么也证明不了（它只是没找到那个名字而已）。

    所以这里逐个进程看**命令行**：
    命中哨兵的特征（``src.gui.server`` 或打包 exe 的路径）才算。

    :param markers: 命令行里出现这些片段就算哨兵。
    :return: 进程个数。
    """
    total = 0
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                pid = int(proc.info.get("pid") or 0)
                if pid == os.getpid():
                    continue
                cmdline = " ".join(
                    str(part) for part in (proc.info.get("cmdline") or [])
                )
                lowered = cmdline.lower()
                # ⚠️ **别把自己算进去** ——
                #    演示脚本的命令行里就带着 exe 路径
                #    （``--exe ...\sentinel.exe``），
                #    不排掉的话「残留进程」永远至少是 1 —— 那是**误报**。
                if "demo_v340_tray" in lowered:
                    continue
                if any(marker.lower() in lowered for marker in markers):
                    total += 1
        except Exception as exc:  # noqa: BLE001 —— 查不到就如实说
            print("      进程枚举失败：{}: {}".format(type(exc).__name__, exc))
            return -1
        return total
    # 没有 psutil —— 退回到按进程名查（只对打包版有效）。
    try:
        output = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
    except (OSError, ValueError):
        return 0
    for line in output.splitlines():
        lowered = line.lower()
        if "sentinel.exe" in lowered or "src.gui.server" in lowered:
            total += 1
    return total


def _port_listening(port: int) -> bool:
    """某个端口还有没有人在听。

    :param port: 端口号。
    :return: 还在监听返回 ``True``。
    """
    try:
        output = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
    except (OSError, ValueError):
        return False
    needle = "127.0.0.1:{} ".format(port)
    for line in output.splitlines():
        if needle in line and "LISTENING" in line:
            return True
    return False


def _build_command(
    root: str,
    work: str,
    config: str,
    port: int,
    exe_path: str = "",
) -> List[str]:
    """拼出启动哨兵的命令行。

    :param root: 项目根目录。
    :param work: 演示用的工作目录。
    :param config: 配置文件路径。
    :param port: 端口。
    :param exe_path: 打包 exe 路径；留空就是源码模式。
    :return: 命令行列表。
    """
    args = [
        "--radar",
        "--config", config,
        "--alert-log", os.path.join(work, "alert.log"),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--no-browser",
        "--no-health",
        "--no-guardian",
    ]
    if exe_path:
        return [exe_path] + args
    return [sys.executable, "-m", "src.gui.server"] + args


def main(argv=None) -> int:
    """命令行入口。

    :param argv: 参数列表。
    :return: 进程退出码。
    """
    _prepare_stdout()
    parser = argparse.ArgumentParser(
        description="v3.4.0 托盘验收演示"
    )
    parser.add_argument(
        "--exe",
        default="",
        help="跑打包好的 exe（留空则用源码模式）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8142,
        help="看板端口",
    )
    args = parser.parse_args(argv)

    dpi_aware = _enable_dpi_awareness()
    print("DPI 感知：{}（坐标与截图同一套物理像素）".format(dpi_aware))

    root = _project_root()
    work = os.path.join(root, "logs", "v340-demo")
    os.makedirs(work, exist_ok=True)

    # ⚠️ 演示必须用**独立的配置** ——
    # 用独立配置就绝不会碰到用户自己的挂载状态。
    config = os.path.join(work, "config.yaml")
    example = os.path.join(root, "config", "config.example.yaml")
    if os.path.exists(example):
        shutil.copyfile(example, config)
    attached = os.path.join(work, "attached_sessions.json")
    if os.path.exists(attached):
        os.remove(attached)

    exe_path = os.path.abspath(args.exe) if args.exe else ""
    if exe_path and not os.path.exists(exe_path):
        print("找不到 exe：{}".format(exe_path))
        return 1

    port = int(args.port)
    base = "http://127.0.0.1:{}".format(port)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server_log = open(
        os.path.join(work, "server.log"), "w", encoding="utf-8"
    )
    command = _build_command(root, work, config, port, exe_path)
    print("启动：{}".format(" ".join(command)))
    proc = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    # 残留核对的特征 —— 源码模式看模块名，打包模式看 exe 路径。
    markers = ["src.gui.server"]
    if exe_path:
        markers = [os.path.basename(exe_path)]
    evidence: Dict[str, Any] = {
        "port": port,
        "mode": "exe" if exe_path else "source",
        "exe": exe_path,
        "dpi_aware": dpi_aware,
        "command": command,
    }
    try:
        print("[1/5] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("看板没起来")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/5] 等托盘图标出现 …")
        tray_windows: List[int] = []
        deadline = time.time() + 30
        while time.time() < deadline:
            tray_windows = _find_tray_windows()
            if tray_windows:
                break
            time.sleep(0.5)
        if not tray_windows:
            print("      没找到托盘图标窗口")
            evidence["tray_found"] = False
            return 1
        evidence["tray_found"] = True
        evidence["tray_windows"] = tray_windows
        print("      找到托盘图标窗口：{}".format(tray_windows))

        print("[3/5] 唤出右键菜单 …")
        cursor = _move_cursor_to_tray()
        print("      鼠标挪到通知区：{}".format(cursor))
        menu_hwnd, items = _show_tray_menu(tray_windows)
        if not menu_hwnd:
            print("      菜单没弹出来")
            evidence["menu_shown"] = False
            return 1
        time.sleep(0.6)
        evidence["menu_shown"] = True
        evidence["menu_items"] = items
        print("      菜单项：")
        for item in items:
            print("        · {}".format(item["text"]))

        assets = os.path.join(root, "_管理文档", "文档与报告", "assets")
        os.makedirs(assets, exist_ok=True)
        shot = os.path.join(assets, "v340-tray-menu.png")
        full = os.path.join(assets, "v340-tray-menu-full.png")
        real_items = [
            item for item in items if str(item.get("text") or "").strip()
        ]
        menu_rect = None
        if real_items:
            lefts = [item["rect"][0] for item in real_items]
            tops = [item["rect"][1] for item in real_items]
            rights = [item["rect"][2] for item in real_items]
            bottoms = [item["rect"][3] for item in real_items]
            menu_rect = (
                min(lefts),
                min(tops),
                max(rights),
                max(bottoms),
            )
        evidence["menu_screen_rect"] = menu_rect
        _grab_screen(full, shot, menu_rect)
        print("      整屏截图 → {}".format(full))

        print("[4/5] 点【彻底退出】 …")
        target = None
        for item in items:
            if "彻底退出" in str(item.get("text") or ""):
                target = item
                break
        if not target:
            print("      菜单里没有【彻底退出】")
            return 1
        clicked = _click_menu_item(target, menu_hwnd)
        evidence["clicked_with_mouse"] = bool(clicked)

        # ⚠️ 鼠标点不中时的兜底 ——
        #    重新唤出菜单，用**键盘**选中同一项。
        #    走的还是 pystray 那条 ``TPM_RETURNCMD`` 的路，
        #    选中的还是【彻底退出】这一项。
        gone = False
        if clicked:
            deadline = time.time() + 10
            while time.time() < deadline:
                if proc.poll() is not None:
                    gone = True
                    break
                time.sleep(0.3)
        if not gone:
            print("      鼠标没点中 —— 用键盘兜底 …")
            menu_hwnd, _items = _show_tray_menu(tray_windows)
            if menu_hwnd:
                time.sleep(0.4)
                # END = 跳到最后一项（就是【彻底退出】），
                # ENTER = 选中它。
                _press_menu_key(0x23)  # VK_END
                time.sleep(0.3)
                _press_menu_key(0x0D)  # VK_RETURN
                evidence["clicked_with_keyboard"] = True
                deadline = time.time() + 15
                while time.time() < deadline:
                    if proc.poll() is not None:
                        gone = True
                        break
                    time.sleep(0.3)

        print("[5/5] 核对残留 …")
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                gone = True
                break
            time.sleep(0.5)
        evidence["process_exited"] = gone
        evidence["exit_code"] = proc.returncode
        # ⚠️ 打包版 exe 是「单文件」—— PyInstaller 会再拉一个**子进程**
        #    干真正的活。父进程退出后，子进程可能还在收尾的那一两秒里。
        #    所以这里**沉降一下**再数 —— 免得把「正在退出」误报成「残留」。
        left = -1
        deadline = time.time() + 15
        while time.time() < deadline:
            left = _count_sentinel_processes(markers)
            if left == 0:
                break
            time.sleep(1.0)
        evidence["sentinel_processes_left"] = left
        evidence["port_still_listening"] = _port_listening(port)
        evidence["tray_window_left"] = bool(_find_tray_windows())
        print("      进程已退出：{}".format(gone))
        print(
            "      残留的哨兵进程：{}".format(
                evidence["sentinel_processes_left"]
            )
        )
        print(
            "      端口 {} 还在监听：{}".format(
                port, evidence["port_still_listening"]
            )
        )
        print(
            "      托盘窗口还在：{}".format(
                evidence["tray_window_left"]
            )
        )

        out = os.path.join(
            root,
            "_管理文档",
            "文档与报告",
            "assets",
            "v340-tray-evidence.json",
        )
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("      证据 → {}".format(out))
        # 再留一份**按形态分**的副本 ——
        # 源码模式与打包版各跑一次，两份证据互不覆盖。
        mode_out = os.path.join(
            root,
            "_管理文档",
            "文档与报告",
            "assets",
            "v340-tray-evidence-{}.json".format(
                "exe" if exe_path else "source"
            ),
        )
        with open(mode_out, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("      证据（按形态）→ {}".format(mode_out))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        server_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
