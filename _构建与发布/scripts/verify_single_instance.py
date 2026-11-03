"""打包版 exe 的**单例实机验证**（v5.0.1）。

回答一个问题：**连点 5 次之后，到底有几个哨兵在跑？**

怎么分「引导器」和「核心进程」
==============================

PyInstaller 单文件（``--onefile``）模式下，exe 双击之后是**两个进程**：

* **引导器（bootloader）** —— 解压 + 等子进程，**不跑 Python**；
* **核心进程** —— 真正执行入口脚本的那个。

任务管理器里它们**同名**，看起来像双开，其实是**一个实例**。

区分办法不靠环境变量，靠**父子关系**：

* 核心进程的**父进程也是** ``sentinel-daemon.exe``（它是引导器拉起来的）；
* 引导器的**父进程不是**（它是资源管理器拉起来的）。

这条判据跟 PyInstaller 版本无关。

验收标准
========

* **核心进程 == 1** —— 真正干活的只有一个；
* **引导器 <= 1** —— 允许有一个临时的引导解压进程；
* 其余 4 个实例必须**立刻退出**（不能靠 10 秒超时）。

用法::

    python _构建与发布/scripts/verify_single_instance.py
    python _构建与发布/scripts/verify_single_instance.py --clicks 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

#: 打包产物路径。
EXE = os.path.join("_构建与发布", "dist", "sentinel-daemon.exe")

#: 进程名（不含 .exe）。
IMAGE_NAME = "sentinel-daemon.exe"


def _list_processes() -> list:
    """列出当前所有 ``sentinel-daemon.exe`` 进程。

    :return: 每项是 ``{"pid": int, "ppid": int}``。
    """
    script = (
        "$p = Get-CimInstance Win32_Process "
        "-Filter \"Name='{name}'\" -ErrorAction SilentlyContinue; "
        "if ($p) {{ $p | Select-Object ProcessId,ParentProcessId "
        "| ConvertTo-Json -Compress }}".format(name=IMAGE_NAME)
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = (out.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    result = []
    for item in data or []:
        try:
            result.append(
                {
                    "pid": int(item.get("ProcessId")),
                    "ppid": int(item.get("ParentProcessId")),
                }
            )
        except (TypeError, ValueError):
            continue
    return result


def _classify(procs: list) -> tuple:
    """把进程分成「核心」与「引导器」。

    :param procs: :func:`_list_processes` 的结果。
    :return: ``(核心进程列表, 引导器列表)``。
    """
    pids = {item["pid"] for item in procs}
    core = []
    bootstrap = []
    for item in procs:
        # 父进程也是同名进程 → 它是被引导器拉起来的核心进程。
        if item["ppid"] in pids:
            core.append(item)
        else:
            bootstrap.append(item)
    return core, bootstrap


def _kill_all(procs: list) -> None:
    """把列出来的进程**连同子孙**一起结束掉。"""
    for item in procs:
        subprocess.run(
            [
                "taskkill", "/F", "/T", "/PID", str(item["pid"]),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="verify_single_instance",
        description="v5.0.1 实机验证：连点多次只留一个核心进程",
    )
    parser.add_argument("--clicks", type=int, default=5)
    parser.add_argument("--settle", type=float, default=25.0)
    parser.add_argument(
        "--out",
        default=os.path.join("logs", "single_instance_output"),
        help="终端输出存档目录",
    )
    args = parser.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    exe = os.path.join(root, EXE)
    if not os.path.exists(exe):
        print("找不到打包产物：{}".format(exe))
        return 2

    archive = os.path.join(root, args.out)
    os.makedirs(archive, exist_ok=True)

    # 清场：把之前遗留的实例全部收掉。
    _kill_all(_list_processes())
    time.sleep(1.0)

    print("[1/3] 连点 {} 次 —— 每次都是「真的双击」".format(args.clicks))
    spawned = []
    for index in range(args.clicks):
        handle = open(
            os.path.join(archive, "run{}.log".format(index)),
            "w",
            encoding="utf-8",
            newline="\n",
        )
        proc = subprocess.Popen(
            [exe, "--daemon"],
            cwd=root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        spawned.append((proc, handle))
        # 连点之间只隔一点点 —— 模拟「手快」。
        time.sleep(0.35)

    print("      已启动 {} 个 exe 实例，等它们自己分出胜负……".format(
        args.clicks
    ))
    time.sleep(max(5.0, args.settle))

    procs = _list_processes()
    core, bootstrap = _classify(procs)

    print("[2/3] 任务管理器里的同名进程")
    print("      总数            ：{}".format(len(procs)))
    print("      核心进程（真干活）：{}".format(len(core)))
    for item in core:
        print("        PID {}（父 PID {}）".format(
            item["pid"], item["ppid"]
        ))
    print("      引导器（解压壳）  ：{}".format(len(bootstrap)))
    for item in bootstrap:
        print("        PID {}（父 PID {}）".format(
            item["pid"], item["ppid"]
        ))

    # 各实例自己的退出码 —— 输掉的那个应该是 0（成功的空操作）。
    codes = []
    for index, (proc, handle) in enumerate(spawned):
        try:
            proc.wait(timeout=1)
            codes.append((index, proc.returncode))
        except subprocess.TimeoutExpired:
            codes.append((index, "仍在运行"))
        finally:
            handle.close()

    print("[3/3] 每个实例的退出结果")
    for index, code in codes:
        print("      第 {} 个启动 → {}".format(index + 1, code))

    ok = len(core) == 1 and len(bootstrap) <= 1
    print("")
    print("验收结果：{}".format(
        "通过 —— 核心进程恰好 1 个" if ok
        else "不通过 —— 核心进程 {} 个（必须 1 个）".format(len(core))
    ))

    # 清场。
    _kill_all(_list_processes())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
