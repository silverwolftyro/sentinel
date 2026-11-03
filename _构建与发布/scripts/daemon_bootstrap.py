"""哨兵打包版的**绝对入口**（v5.0.1）。

这个文件刻意做到**极小** —— 它只做三件事：

1. 把仓库根与脚本目录塞进 ``sys.path``；
2. **抢进程级单例锁**（Windows 命名互斥体）；
3. 抢不到就打印提示并 ``sys.exit(0)``；抢到了才去 import 真正的入口。

为什么要单独开一个文件
======================

v5.0.0 之前，入口是 :mod:`sentinel_entry`，它是一个 **55 KB** 的模块，
模块级就 import 了一堆东西。单例锁是在它**内部**抢的 ——
也就是说，**大模块已经被加载了**，锁才拿到。

v5.0.1 把锁**下沉到这里**：这个文件只 import 标准库和
:mod:`src.core.singleton`（它也只读标准库），
所以抢锁发生在**任何大模块被加载、任何线程被创建之前**。

⚠️ **抢锁失败必须 ``sys.exit(0)``** ——
   「已经有实例在跑」对无头守护进程来说是一次**成功的空操作**，
   不是启动失败。用非 0 退出码会让看门狗/脚本误判成故障。

关于 PyInstaller 单文件的父子进程
================================

``--onefile`` 模式下，exe 双击后是**两个进程**：

* **父进程（bootloader）** —— 只负责把包解压到 ``%TEMP%``，
  然后等子进程退出，自己也跟着退出。**它不跑任何 Python 代码**；
* **子进程** —— 真正执行本文件、跑入口脚本。

所以 **单例锁天然就落在核心执行进程上** —— 引导器根本碰不到它。
本文件另外还会把这件事**记录下来**（见
:func:`src.core.singleton.core_process_info`），
以便实机验证时能区分「引导器」与「核心进程」。

⚠️ 任务管理器里因此会出现**两个同名进程** ——
   那是**一个实例**，不是双开。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

# 让 ``src.*`` 与 ``sentinel_entry`` 都能被找到。
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# ⚠️ 必须在 import 任何大模块之前抢锁。
from src.core.singleton import (  # noqa: E402
    ALREADY_RUNNING_EXIT_CODE,
    acquire_single_instance,
    report_already_running,
)

#: 这些参数**不上锁** —— 与 ``sentinel_entry._dispatch`` 保持一致：
#:
#: * ``--help`` / ``--version`` 必须**永远**能跑 ——
#:   打包脚本的自检就是「再跑一次 ``sentinel-daemon.exe --help``」；
#: * ``--scan`` 是一次性命令行扫描，不常驻，本来就没有双开风险。
_NO_LOCK_FLAGS = (
    "-h",
    "--help",
    "help",
    "-V",
    "--version",
    "version",
)


def _needs_lock() -> bool:
    """这次启动要不要上单例锁。

    :return: 要上锁返回 ``True``。
    """
    args = list(sys.argv[1:])
    if any(item in _NO_LOCK_FLAGS for item in args):
        return False
    return "--scan" not in args


def bootstrap_guard() -> bool:
    """在**最早期**抢单例锁。

    :return: 拿到锁（或不需要锁）返回 ``True``；
        **已经有实例在跑**返回 ``False``。
    """
    if not _needs_lock():
        return True
    if acquire_single_instance() is None:
        report_already_running()
        return False
    return True


if __name__ == "__main__":
    if not bootstrap_guard():
        # ⚠️ 「已经在跑」= 成功的空操作 → 退出码 0。
        raise SystemExit(ALREADY_RUNNING_EXIT_CODE)
    # ⚠️ 抢到锁之后才 import 真正的入口 —— 大模块一律晚加载。
    from sentinel_entry import main  # noqa: E402

    raise SystemExit(main())
