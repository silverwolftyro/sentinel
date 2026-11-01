"""双击 sentinel.exe 的实机验证脚本（v2.2.1）。

做三件事：

1. **不带任何参数**启动打包版 exe（= 用户双击）；
2. 把它的终端输出**原样**存下来；
3. 数一数「体检报告」出现了几次 —— 必须**只有一次**。

用法::

    python _构建与发布/scripts/verify_double_click.py --port 8000
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time
import urllib.request

#: 打包产物路径。
EXE = os.path.join("_构建与发布", "dist", "sentinel.exe")


def _count(text: str, needle: str) -> int:
    """数一数某个片段出现了几次。"""
    return text.count(needle)


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="verify_double_click",
        description="v2.2.1 实机验证：双击 exe 不闪退",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--wait", type=float, default=25.0)
    parser.add_argument(
        "--out",
        default=os.path.join("logs", "double_click_output.txt"),
        help="终端输出存档",
    )
    args = parser.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    exe = os.path.join(root, EXE)
    if not os.path.exists(exe):
        print("找不到打包产物：{}".format(exe))
        return 2

    print("[1/3] 不带任何参数启动（= 用户双击）：{}".format(exe))
    archive = os.path.join(root, args.out)
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    # ⚠️ 输出**直接重定向到文件**，不走管道 ——
    # 管道有块缓冲，进程被杀时缓冲区里的内容会丢。
    handle = io.open(archive, "w", encoding="utf-8", newline="\n")
    proc = subprocess.Popen(
        [exe],
        cwd=root,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    # --- 等看板起来 ---------------------------------------------------
    url = "http://127.0.0.1:{}/api/state".format(args.port)
    alive = False
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    alive = True
                    break
        except Exception:  # noqa: BLE001 —— 还没起来是正常的
            time.sleep(0.5)

    print("[2/3] 看板：{}".format("已就绪 HTTP 200" if alive else "没起来"))
    print("      进程：{}".format(
        "还活着（PID {}）".format(proc.pid) if proc.poll() is None
        else "已经退出（退出码 {}）".format(proc.returncode)
    ))

    # --- 收输出 --------------------------------------------------------
    time.sleep(1.0)
    # ⚠️ 必须杀**整棵进程树**（v2.2.2 修正）。
    #
    # PyInstaller 单文件模式会**再起一个子进程**跑真正的程序：
    # 启动器（bootloader）只是壳。只 terminate 启动器的话，
    # 那个子进程会**活下来**继续占着端口 —— 下一次验证就会撞上
    # 「端口被占用」。
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        proc.wait(timeout=10)
    except Exception as exc:  # noqa: BLE001 —— 收不到就算了
        print(
            "结束验证用进程失败（PID {}）：{}: {}".format(
                proc.pid, type(exc).__name__, exc
            ),
            file=sys.stderr,
        )
    handle.close()
    with io.open(archive, "r", encoding="utf-8") as reader:
        text = reader.read()

    reports = _count(text, "哨兵安全体检报告")
    summaries = [
        line for line in text.splitlines() if line.startswith("正常 ")
    ]
    errors = _count(text, "必须给 --watch")

    print("[3/3] 终端输出已存档：{}".format(archive))
    print("      体检报告出现次数：{}  ← 必须等于 1".format(reports))
    print("      统计行出现次数　：{}  ← 必须等于 1".format(len(summaries)))
    print("      「必须给 --watch」错误：{}  ← 必须等于 0".format(errors))
    if summaries:
        print("      统计行内容　　　：{}".format(summaries[0]))
    print("")
    print("终端输出全文：")
    print("-" * 62)
    # ⚠️ 输出里有 ``⚠️`` —— 控制台是 GBK 的话 print 会炸。
    # 这里切到 UTF-8 再打。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(
                    encoding="utf-8", errors="replace", line_buffering=True
                )
            except (ValueError, OSError, AttributeError):
                pass
    print(text)
    print("-" * 62)

    ok = alive and reports == 1 and len(summaries) == 1 and errors == 0
    print("验证结果：{}".format("通过" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
