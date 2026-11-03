"""v3.0.1 回归：演示退出 · 设置页分栏 · 雷达配置写回。

三块：

1. **演示模式能真的退出**（任务一）——
   横幅上有【退出演示，开始真实扫描】按钮；点它之后
   演示进程会停下来，页面给出**能直接复制**的真命令；
2. **设置页是左右分栏**（任务二）——
   左边 240px 导航，右边全宽内容，卡片不再自己滚，
   窄屏下导航变顶部横向 Tab；
3. **雷达扫描根目录能写回配置**（任务三）——
   设置页填的路径要能落到 ``config.yaml`` 的 ``radar.scan_roots``。
"""

from __future__ import annotations

import os

from src.console import (
    DEMO_KEEP_OPEN_LINES,
    DEMO_REMINDER_INTERVAL,
    DEMO_REMINDER_LINE,
    DEMO_REMINDER_TIMES,
)
from src.core.session_server import (
    DEMO_EXIT_INSTRUCTIONS,
    _collect_config_updates,
)

#: 项目根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    """读仓库里的一个文件。

    :param parts: 相对项目根的路径片段。
    :return: 文件文本。
    """
    path = os.path.join(ROOT, *parts)
    with open(path, encoding="utf-8") as handle:
        return handle.read()
















def test_退出演示指令常量非空():
    """引导指令不能是空的 —— 空了就等于没引导。

    ⚠️ 里面**允许**有空行（分段用），但不能有「只有空白」的行。
    """
    assert DEMO_EXIT_INSTRUCTIONS
    lines = [str(item) for item in DEMO_EXIT_INSTRUCTIONS]
    assert any(item.strip() for item in lines)
    for item in lines:
        # 命令行前面**要**有缩进（复制出来才像命令），
        # 但行尾不能有空白 —— 那是复制不干净的来源。
        assert item == item.rstrip()
        assert not (item and not item.strip())


def test_退出演示指令是纯文本():
    """前端用 ``textContent`` 显示 —— 不能夹带 Markdown 标记。"""
    text = "\n".join(str(item) for item in DEMO_EXIT_INSTRUCTIONS)

    assert "**" not in text
    assert "`" not in text




def test_终端有别关窗口的醒目块():
    """终端必须有一块醒目的「不要关闭此窗口」。"""
    text = "\n".join(DEMO_KEEP_OPEN_LINES)
    assert "不要关闭此窗口" in text
    assert "看板" in text


def test_终端提醒会重复若干次():
    """「反复提示」= 间隔 + 次数都要有，不能只喊一遍。"""
    assert DEMO_REMINDER_INTERVAL > 0
    assert DEMO_REMINDER_TIMES >= 3
    assert "不要关闭此窗口" in DEMO_REMINDER_LINE


















# ---------------------------------------------------------------------------
# 3. 雷达扫描根目录：配置写回
# ---------------------------------------------------------------------------
def test_扫描根目录能翻译成配置路径():
    """设置页的表单要翻译成 ``radar.scan_roots``。"""
    updates = _collect_config_updates(
        {"radar": {"scan_roots": [".codex", "D:\\项目", "  "]}}
    )

    assert ("radar.scan_roots", [".codex", "D:\\项目"]) in updates


def test_没有雷达段就不产生更新():
    """没填就不该写。"""
    updates = _collect_config_updates({"llm": {"model": "x"}})

    assert not [item for item in updates if item[0].startswith("radar")]










def test_配置模板里有雷达段():
    """``config.example.yaml`` 必须带 ``radar.scan_roots``。"""
    text = _read("config", "config.example.yaml")

    assert "radar:" in text
    assert "scan_roots:" in text
    assert ".codex" in text
    assert ".claude" in text
