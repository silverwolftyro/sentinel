"""自启残留检测（v3.3.0）。

为什么要有这个模块
==================

哨兵 v3.1.0 ~ v3.2.1 提供过「开机自启」功能：
在 Windows 的「启动」文件夹里放一个快捷方式，
指向一个隐藏窗口的 VBS，开机就把哨兵**静默**拉起来。

v3.3.0 起，这个功能被**彻底删除** —— 用户明确要求
「**拒绝任何形式的开机自启动和后台静默驻留**」。

但光删代码不够：**已经装过的机器上还有残留**。
快捷方式还在「启动」文件夹里，VBS 还在 dist 目录里，
用户下次开机照样会被静默拉起来 —— 而他以为早就删干净了。

所以哨兵现在**每次启动都主动看一眼**：
发现残留就**显眼地**告诉用户，并给出清理命令。

⚠️ 这个模块**只读不写**
----------------------

它**只检测、只提示**，不替用户删东西 ——
删用户系统里的文件是**用户的决定**，哨兵不越俎代庖。
真要删，跑 ``_构建与发布/scripts/remove_startup_residue.ps1``。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

__all__ = [
    "RESIDUE_LINK_NAME",
    "detect_residue",
    "render_notice",
]

#: 「启动」文件夹里那个快捷方式的名字。
RESIDUE_LINK_NAME = "哨兵 Sentinel.lnk"

#: 隐藏启动器 VBS 的名字（打包版与源码版各一个位置）。
RESIDUE_VBS_NAMES = (
    os.path.join("dist", "sentinel_startup.vbs"),
    os.path.join("logs", "sentinel_startup.vbs"),
)


def _startup_dir() -> str:
    """拿「启动」文件夹的路径。

    :return: 路径字符串；拿不到就返回空串。
    """
    try:
        return os.environ.get("APPDATA", "") and os.path.join(
            os.environ["APPDATA"],
            "Microsoft",
            "Windows",
            "Start Menu",
            "Programs",
            "Startup",
        )
    except (KeyError, OSError):
        return ""


def _run_key_residue() -> List[Dict[str, str]]:
    """检查注册表 Run 键里有没有哨兵条目。

    ⚠️ v3.1.0 的自启用的是**「启动」文件夹**，**没碰注册表** ——
    但用户可能自己加过。这里**只查不改**。

    :return: 命中的条目列表（每条含 ``location`` / ``name`` / ``value``）。
    """
    found: List[Dict[str, str]] = []
    if os.name != "nt":
        return found
    try:
        import winreg
    except ImportError:  # pragma: no cover —— 非 Windows 平台
        return found
    for root, hive in (
        ("HKCU", winreg.HKEY_CURRENT_USER),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE),
    ):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(hive, key_path) as key:
                index = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    haystack = "{} {}".format(name, value).lower()
                    if "哨兵" in haystack or "sentinel" in haystack:
                        found.append(
                            {
                                "location": "{}\\{}".format(root, key_path),
                                "name": name,
                                "value": value,
                            }
                        )
        except OSError:
            # 键不存在 / 没权限 —— 当作「没有」。
            continue
    return found


def detect_residue(
    project_root: Optional[str] = None,
) -> List[Dict[str, str]]:
    """检查机器上还留着哪些哨兵**自启残留**。

    ⚠️ **只检测，不删除** —— 删除是用户的决定。

    :param project_root: 项目根目录（默认取当前工作目录）。
    :return: 残留清单；每条含 ``kind`` / ``path`` / ``how_to_clean``。
    """
    residue: List[Dict[str, str]] = []

    startup = _startup_dir()
    if startup:
        link = os.path.join(startup, RESIDUE_LINK_NAME)
        if os.path.exists(link):
            residue.append(
                {
                    "kind": "快捷方式",
                    "path": link,
                    "how_to_clean": "删掉这个文件，或跑清理脚本。",
                }
            )

    root = project_root or os.getcwd()
    for relative in RESIDUE_VBS_NAMES:
        path = os.path.join(root, relative)
        if os.path.exists(path):
            residue.append(
                {
                    "kind": "隐藏启动器",
                    "path": path,
                    "how_to_clean": "删掉这个文件（它只会隐藏窗口启动哨兵）。",
                }
            )

    residue.extend(
        {
            "kind": "注册表启动项",
            "path": "{} → {}".format(item["location"], item["name"]),
            "how_to_clean": (
                "在「任务管理器 → 启动」里禁用，或删掉这个注册表值。"
            ),
        }
        for item in _run_key_residue()
    )

    return residue


def render_notice(residue: Optional[List[Dict[str, str]]] = None) -> str:
    """把残留清单渲染成一段**显眼**的终端提示。

    :param residue: 残留清单；``None`` 表示现场检测一次。
    :return: 多行文本；没有残留时返回空串。
    """
    items = detect_residue() if residue is None else residue
    if not items:
        return ""
    lines = [
        "",
        "!" * 62,
        "  ⚠️  这台机器上还留着**哨兵的开机自启残留**！",
        "!" * 62,
        "",
        "  v3.3.0 起哨兵**不再有**开机自启功能 ——",
        "  「用完即走，想开才开」，不会在开机时静默驻留。",
        "  但下面这些东西是**旧版本**留下的，不删的话下次开机还会被拉起来：",
        "",
    ]
    for item in items:
        lines.append("    · [{}] {}".format(item["kind"], item["path"]))
        lines.append("      {}".format(item["how_to_clean"]))
    lines += [
        "",
        "  一次清干净（管理员 PowerShell 里跑）：",
        "",
        '    powershell -NoProfile -ExecutionPolicy Bypass -File "_构建与发布\\scripts\\remove_startup_residue.ps1"',
        "",
        "  ⚠️ 不想动手也行：把「启动」文件夹里的「{}」删掉即可。".format(
            RESIDUE_LINK_NAME
        ),
        "!" * 62,
        "",
    ]
    return "\n".join(lines)
