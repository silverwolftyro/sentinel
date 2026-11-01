"""演示模式显性化的实机验证脚本（v2.2.2）。

做四件事：

1. **不带任何参数**启动打包版 exe（= 用户双击 → 演示模式）；
2. 把它的终端输出**原样**存下来，检查**6 行醒目说明**在不在，
   以及它是不是排在 ASCII 大字报**后面**；
3. 抓看板首页的 HTML，检查**红色横幅**在不在；
4. 抓 ``/api/sessions``，检查会话名是不是「**[演示] 默认会话**」。

⚠️ 为什么要专门写这个脚本
=======================

v2.2.2 要修的正是「用户被界面骗了」这件事。
如果只靠单元测试，就只验证了**字符串**；
用户真正看到的是**打包后的 exe** + **浏览器里的页面** ——
那才是会骗人的地方。所以必须**实机**看一遍。

用法::

    python _构建与发布/scripts/verify_demo_banner.py --port 8000
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

#: 打包产物路径。
EXE = os.path.join("_构建与发布", "dist", "sentinel.exe")

#: 看板红色横幅的**逐字**文案。
BANNER_TEXT = "⚠ 当前为演示模式，展示的是模拟数据，未监控任何真实进程！"

#: 终端说明里的关键行。
NOTICE_MARK = "注意：您当前进入的是【演示模式】！"


def _banner_before_notice(text: str) -> bool:
    """大字报是不是排在演示说明**前面**。

    ⚠️ 大字报是**块状字模**（``█``），不是 ``ACTIVE`` 字样 ——
    所以不能拿 ``"ACTIVE"`` 去定位，要找含 ``█`` 的行。
    """
    notice_pos = text.find(NOTICE_MARK)
    if notice_pos < 0:
        return False
    for line in reversed(text[:notice_pos].splitlines()):
        if "█" in line:
            return True
    return False


def _fetch(url: str, timeout: float = 3.0):
    """抓一个 URL；失败返回 ``None``。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 还没起来是正常的
        return None


def _prepare_stdout() -> None:
    """控制台是 GBK 的话，打中文会炸 —— 先切到 UTF-8。"""
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


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="verify_demo_banner",
        description="v2.2.2 实机验证：演示模式必须显式说破",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--wait", type=float, default=30.0)
    parser.add_argument(
        "--out",
        default=os.path.join("logs", "demo_banner_output.txt"),
        help="终端输出存档",
    )
    args = parser.parse_args(argv)
    _prepare_stdout()

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    exe = os.path.join(root, EXE)
    if not os.path.exists(exe):
        print("找不到打包产物：{}".format(exe))
        return 2

    print("[1/4] 不带任何参数启动（= 用户双击）：{}".format(exe))
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
    base = "http://127.0.0.1:{}".format(args.port)
    alive = False
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _fetch(base + "/api/state") is not None:
            alive = True
            break
        time.sleep(0.5)

    html = _fetch(base + "/") or ""
    sessions_raw = _fetch(base + "/api/sessions") or ""

    # --- 收输出 --------------------------------------------------------
    time.sleep(1.0)
    # ⚠️ 必须杀**整棵进程树**。
    #
    # PyInstaller 单文件模式会**再起一个子进程**跑真正的程序：
    # 启动器（bootloader）只是壳。只 terminate 启动器的话，
    # 那个子进程会**活下来**继续占着端口 —— 下一次验证就会撞上
    # 「端口被占用」，而且它的 stdout 还攥着上一个存档文件，
    # 把新存档冲成一堆空洞。
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

    # --- 判定 ----------------------------------------------------------
    notice_ok = NOTICE_MARK in text
    banner_ok = BANNER_TEXT in html
    label_ok = False
    if sessions_raw:
        try:
            payload = json.loads(sessions_raw)
            label_ok = any(
                str(item.get("label", "")).startswith("[演示]")
                for item in payload.get("sessions", [])
            )
        except (ValueError, AttributeError):
            label_ok = False

    # 顺序：大字报（块状字模）必须在演示说明**前面**。
    order_ok = _banner_before_notice(text)

    print("[2/4] 看板：{}".format("已就绪 HTTP 200" if alive else "没起来"))
    print("[3/4] 终端输出已存档：{}".format(archive))
    print("      6 行演示说明在不在　：{}".format("在" if notice_ok else "❌ 不在"))
    print("      大字报在说明之前吗　：{}".format(
        "是" if order_ok else "❌ 不是"
    ))
    print("[4/4] 看板红色横幅在不在　：{}".format(
        "在" if banner_ok else "❌ 不在"
    ))
    print("      会话名带「[演示]」吗：{}".format(
        "是" if label_ok else "❌ 不是"
    ))

    print("")
    print("终端输出全文：")
    print("-" * 62)
    print(text)
    print("-" * 62)

    ok = alive and notice_ok and order_ok and banner_ok and label_ok
    print("验证结果：{}".format("通过" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
