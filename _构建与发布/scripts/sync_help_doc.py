"""把 ``sentinel.exe --help`` 的实际输出回填到帮助文档里。

为什么要有这个脚本
==================

``sentinel-命令行帮助.md`` 里贴的是**帮助全文** ——
它必须和 exe 的真实输出一致，否则文档就在说假话。

⚠️ 用法：``python _构建与发布/scripts/sync_help_doc.py``。
"""

from __future__ import annotations

import difflib
import io
import os
import re
import subprocess
import sys


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


def main(argv=None) -> int:
    """命令行入口。"""
    _prepare_stdout()
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    exe = os.path.join(root, "_构建与发布", "dist", "sentinel.exe")
    doc = os.path.join(
        root, "_管理文档", "文档与报告", "sentinel-命令行帮助.md"
    )
    if not os.path.isfile(exe):
        print("找不到打包产物：{}".format(exe))
        return 2
    if not os.path.isfile(doc):
        print("找不到帮助文档：{}".format(doc))
        return 2

    result = subprocess.run(
        [exe, "--help"], capture_output=True, cwd=root
    )
    help_text = result.stdout.decode("utf-8", errors="replace")
    help_text = help_text.replace("\r\n", "\n").rstrip("\n")

    with io.open(doc, "r", encoding="utf-8") as handle:
        text = handle.read()

    pattern = re.compile(r"(```text\n)(.*?)(\n```)", re.S)
    match = pattern.search(text)
    if match is None:
        print("文档里找不到 ```text 帮助全文块。")
        return 2

    old = match.group(2)
    if old.strip() == help_text.strip():
        print("帮助全文已经是最新的 —— 无需改动。")
        return 0

    updated = text[:match.start(2)] + help_text + text[match.end(2):]
    with io.open(doc, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)

    print("帮助全文已更新（{} → {} 字符）。".format(
        len(old), len(help_text)
    ))
    for line in list(difflib.unified_diff(
        old.splitlines(), help_text.splitlines(), lineterm=""
    ))[:60]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
