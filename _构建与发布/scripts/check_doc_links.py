"""文档链接完整性检查：扫全部 ``.md`` 的相对链接，逐个验证目标是否存在。

目录一动，链接就容易烂掉 —— v0.6.0 迁移目录时就一次抓出 **7 条坏链**。
所以把这件事固化成一条命令：**改完目录必须跑一次**。

只检查**相对链接**（本地文件）；``http(s)`` / ``mailto:`` / 纯锚点一律跳过。

用法::

    python _构建与发布/scripts/check_doc_links.py
    python _构建与发布/scripts/check_doc_links.py --root ../
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Optional, Tuple

#: Markdown 行内链接：``[文字](目标)``。
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

#: 跳过的目录（不入库、与文档无关）。
SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    "回收站",
    "_备份与归档",
}


def _iter_markdown(root: str) -> List[str]:
    """列出全部 ``.md`` 文件。

    :param root: 扫描根目录。
    :return: 文件路径列表。
    """
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(
            os.path.join(dirpath, name)
            for name in filenames
            if name.endswith(".md")
        )
    return found

def check(root: str) -> Tuple[int, List[Tuple[str, str]]]:
    """检查全部相对链接。

    :param root: 扫描根目录。
    :return: ``(检查总数, 坏链列表)``；坏链元素是 ``(源文件, 目标)``。
    """
    total = 0
    broken: List[Tuple[str, str]] = []
    for path in _iter_markdown(root):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#")[0]
            if not target:
                continue
            total += 1
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if not os.path.exists(resolved):
                broken.append(
                    (os.path.relpath(path, root), target)
                )
    return total, broken


def _prepare_stdout() -> None:
    """让重定向 / 管道场景下的中文输出不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(
                    encoding="utf-8", errors="replace", line_buffering=True
                )
            except (ValueError, OSError, AttributeError):
                pass


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码（0 = 无坏链）。
    """
    parser = argparse.ArgumentParser(
        prog="check_doc_links",
        description="检查全部 Markdown 相对链接是否指向存在的文件",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="扫描根目录，默认当前目录",
    )
    args = parser.parse_args(argv)

    _prepare_stdout()
    root = os.path.abspath(args.root)
    total, broken = check(root)
    print(f"扫描相对链接 {total} 条")
    if broken:
        print(f"坏链 {len(broken)} 条：")
        for source, target in broken:
            print(f"  {source} -> {target}")
        return 1
    print("0 坏链")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
