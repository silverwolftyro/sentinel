"""跑一次开箱安全体检，把终端报告存成 UTF-8 文件。

为什么要写文件再读：Windows 控制台默认 GBK，
报告里那个 ``⚠️`` 直接 print 会 ``UnicodeEncodeError``。
写盘（UTF-8）再读回来，既能存档，也绕开了编码问题。

用法::

    python _构建与发布/scripts/demo_v22_health.py --out logs/health_report.txt
"""

from __future__ import annotations

import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)))

from src.config_loader import load_config  # noqa: E402
from src.core.health import render_terminal, run_health_check  # noqa: E402


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="demo_v22_health",
        description="v2.2.0 开箱安全体检 —— 打印终端报告并存档",
    )
    parser.add_argument(
        "--out",
        default=os.path.join("logs", "health_report.txt"),
        help="报告存档路径（UTF-8）",
    )
    parser.add_argument(
        "--workdir",
        default="",
        help="被体检的工作目录；留空用当前目录",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="跳过要发网络请求的两项检查",
    )
    args = parser.parse_args(argv)

    config = load_config(None)
    items = run_health_check(
        config,
        workdir=args.workdir or os.getcwd(),
        skip_network=bool(args.skip_network),
    )
    text = render_terminal(items)

    # --- 存一份 UTF-8 存档 -------------------------------------------------
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")

    # --- 打到终端 ----------------------------------------------------------
    # 先把 stdout 切到 UTF-8 —— Windows 控制台默认 GBK，
    # 报告里那个 ``⚠️`` 直接 print 会 UnicodeEncodeError。
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
    print(text)
    print("[DEMO] 报告已存档：{}".format(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
