"""一次性脚本：把 ``启动哨兵.bat`` 的无参数分支改成走启动菜单。

⚠️ 为什么要用脚本改而不是直接编辑
=================================
``.bat`` 文件在仓库里存的是 LF，检出到 Windows 会转成 **CRLF** ——
用普通文本编辑很容易把换行符搞成混的（LF/CRLF 混排会让
``cmd.exe`` 在某些行上出怪问题）。

所以这里用二进制读、字符串替换、再用**原来的换行符**写回去。

用法：``python _构建与发布/scripts/patch_start_bat.py``
"""

from __future__ import annotations

import os
import sys

#: 仓库根目录。
ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

#: 要改的 bat 文件。
TARGET = os.path.join(ROOT, "启动哨兵.bat")

#: 旧的无参数分支。
OLD = """if "%~1"=="" (
    echo   [..] 未检测到参数 —— 进入**演示模式**。
    echo        会生成一段模拟日志并打开看板，让你先看看哨兵长什么样。
    echo        如需监控真实 Agent，请带参数运行，例如：
    echo            启动哨兵.bat --agent codex "在当前目录创建 hello.py"
    echo.
    "%PYEXE%" %PYARGS% -m src.gui.server --demo
    goto :finish
)
"""

#: 新的无参数分支（走入口脚本 → 打印启动菜单）。
NEW = """if "%~1"=="" (
    rem ⚠️ 无参数 = **先问用户想干什么**（v3.0.2）。
    rem 以前这里是直接进演示模式 —— 用户以为「雷达扫不到真实的进程」。
    rem 现在走入口脚本，终端里会打印三个选项：
    rem   [1] 启动演示模式  [2] 开启雷达  [3] 接管并启动新的 Agent
    echo   [..] 未检测到参数 —— 接下来会让你选要做什么。
    echo.
    "%PYEXE%" %PYARGS% "%ROOT%\\_构建与发布\\scripts\\sentinel_entry.py"
    goto :finish
)
"""


def main() -> int:
    """改写 bat 文件。

    :return: 进程退出码。
    """
    with open(TARGET, "rb") as handle:
        raw = handle.read()

    newline = b"\r\n" if b"\r\n" in raw else b"\n"
    text = raw.decode("utf-8")
    old_block = OLD.replace("\n", newline.decode("ascii"))
    new_block = NEW.replace("\n", newline.decode("ascii"))

    if old_block not in text:
        if new_block in text:
            print("已经是新版了，不用改。")
            return 0
        print("没找到要替换的旧分支 —— bat 文件可能已经改过。")
        return 1

    text = text.replace(old_block, new_block)

    # 用原来的换行符写回去。
    body = text.replace("\r\n", "\n")
    with open(TARGET, "wb") as handle:
        handle.write(body.replace("\n", newline.decode("ascii")).encode("utf-8"))

    print("已更新：", TARGET)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
