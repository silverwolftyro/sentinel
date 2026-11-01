"""版本号一致性自检 —— 四处必须一模一样。

版本号散落在四个地方，任何一处漏改都会让用户看到不一样的版本：

1. ``src/__init__.py`` 的 ``__version__``；
2. ``CHANGELOG.md`` 顶部的条目；
3. ``_管理文档/文档与报告/versions/`` 下的版本文档；
4. Git 标签。

用法::

    python _构建与发布/scripts/check_version_consistency.py
"""

from __future__ import annotations

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


def _read(path: str) -> str:
    """读一个文本文件。"""
    with io.open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def main(argv=None) -> int:
    """命令行入口。"""
    _prepare_stdout()
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))

    values = {}

    init_text = _read(os.path.join(root, "src", "__init__.py"))
    match = re.search(r'__version__\s*=\s*"([0-9.]+)"', init_text)
    values["src/__init__.py"] = match.group(1) if match else "(没找到)"

    changelog = _read(os.path.join(root, "CHANGELOG.md"))
    match = re.search(r"## \[v([0-9.]+)\]", changelog)
    values["CHANGELOG.md"] = match.group(1) if match else "(没找到)"

    versions_dir = os.path.join(
        root, "_管理文档", "文档与报告", "versions"
    )
    newest = ""
    newest_file = ""
    for name in sorted(os.listdir(versions_dir)):
        if not name.endswith(".md"):
            continue
        text = _read(os.path.join(versions_dir, name))
        match = re.search(r"\| 版本号 \| `v([0-9.]+)`", text)
        if not match:
            continue
        version = match.group(1)
        if version not in newest:
            newest = version
            newest_file = name
    values["versions/"] = "{}（{}）".format(newest, newest_file)

    entry = _read(
        os.path.join(root, "_构建与发布", "scripts", "sentinel_entry.py")
    )
    match = re.search(r'__version__\s*=\s*"([0-9.]+)"', entry)
    values["sentinel_entry.py（回退值）"] = (
        match.group(1) if match else "(没找到)"
    )

    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=root, stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 —— 还没打标签是正常的
        tag = "(还没打标签)"
    values["Git 标签"] = tag

    print("版本号四处一致性：")
    for key, value in values.items():
        print("  {:32s} {}".format(key, value))

    core = {
        values["src/__init__.py"],
        values["CHANGELOG.md"],
        values["versions/"].split("（")[0],
    }
    if len(core) == 1 and "(没找到)" not in core:
        print("\n一致：{}".format(core.pop()))
        return 0
    print("\n⚠️ 不一致 —— 请逐处核对。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
