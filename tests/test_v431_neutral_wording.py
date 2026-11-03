"""v4.3.1 回归：文案中性化与开源合规致谢（v5.0.0 更新）。

背景
====
v4.3.0 的雷达功能实机验证成功，但界面上的那个**特定上游项目的私人称呼**
过于私人化 —— 对「哨兵是一个通用工具」这件事没有好处。

v4.3.1 把这些文案中性化，并按 MIT 协议补上出处声明。

⚠️ v5.0.0 更新
===============

面板（FastAPI + Jinja2 + SSE + 前端静态资源）**整体剥离**之后：

* 「前端不许出现旧称呼」这一类断言**没有对象可测了** ——
  不是「通过了」，是**前端不存在了**。
  下面用 :func:`test_前端资产已经剥离` 把这件事**钉成断言**：
  谁要是把前端加回来，这条测试会失败。
* 后端清单里的 ``src/gui/server.py`` 搬到了
  ``src/core/session_server.py``（核心逻辑不该住在 ``gui/`` 里）。

三条线必须同时成立
==================

1. **后端与文档不许出现私人称呼**；
2. **识别能力不能被改坏** ——
   ``src/proc_tree.py`` 的 ``BRIDGE_HOST_*_MARKERS`` 里那些字面量
   是**识别真实宿主进程的判据**，一个字都不能动；
3. **全库零残留**（v4.3.2 追加）——
   那个私人称呼被判定为「不宜出现在公开仓库的任何角落」，
   于是**历史文档、CHANGELOG、版本说明一并去个人化**。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest

#: 仓库根目录。
ROOT_DIR = Path(__file__).resolve().parents[1]

#: 后端「源代码区」—— 注释与日志里不许出现私人称呼，
#: 但**允许**保留上游项目的出处标识（``NOTICE`` 指向它）。
BACKEND_FILES: Tuple[str, ...] = (
    "src/proc_tree.py",
    "src/procscan.py",
    "src/radar.py",
    "src/core/session_server.py",
    "src/agents/__init__.py",
    "src/agents/launcher.py",
    "src/agents/cloud_agents.json",
    "src/adapters/text.py",
    "src/adapters/fingerprint.py",
)

#: 私人化的旧称呼 —— **一个字都不该再出现**。
#:
#: ⚠️ 这里用 **Unicode 转义**写，不直接写出来 ——
#: 本仓库要求**全库零残留**，连「禁止这个词」的常量本身
#: 都不能把它写进文件，否则全局搜索永远归不了零。
#: （第一个字 U+4E91，第二个字 U+6735）
FORBIDDEN_PRIVATE: Tuple[str, ...] = ("\u4e91\u6735",)


def _read(relative: str) -> str:
    """读一个仓库内文件。"""
    return (ROOT_DIR / relative).read_text(encoding="utf-8")


def _tracked_files() -> List[Path]:
    """列出 Git 正在追踪的所有文件。

    为什么要按 **Git 的追踪清单**来查，而不是扫整个工作区 ——
    未跟踪的本地文件只要 ``.gitignore`` 生效就**不会**进仓库；
    **已跟踪**的才是真正会被公开的那一批。

    :return: 已跟踪文件的绝对路径列表。
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(ROOT_DIR),
    )
    return [ROOT_DIR / item for item in out.stdout.split("\0") if item]


# ---------------------------------------------------------------------------
# 前端资产已剥离（v5.0.0）
# ---------------------------------------------------------------------------
def test_前端资产已经剥离() -> None:
    """面板**整体**没有了 —— 这是 v5.0.0 的核心事实，钉住它。

    以前这里有一堆「前端不许出现旧称呼」的断言；
    面板剥离之后**没有对象可测**了 ——
    那个断言会变成「对空气做检查」。

    所以改成**断言前端不存在**：
    谁要是把面板加回来（或者只加回一半），这条测试会失败。
    """
    for relative in (
        "src/gui/static/dashboard.js",
        "src/gui/static/dashboard.css",
        "src/gui/templates/dashboard.html",
        "src/gui/server.py",
        "src/gui/tray.py",
    ):
        assert not (ROOT_DIR / relative).exists(), (
            "{} 还在 —— v5.0.0 应当已剥离面板".format(relative)
        )


def test_核心逻辑搬到了core() -> None:
    """核心逻辑不该住在 ``gui/`` 里（v5.0.0 顺手搬了家）。"""
    path = ROOT_DIR / "src" / "core" / "session_server.py"
    assert path.is_file(), "核心模块没搬到 src/core/session_server.py"
    source = path.read_text(encoding="utf-8")
    for name in ("AlertStore", "EventHub", "Watcher", "SessionRegistry"):
        assert name in source, name


def test_无头守护进程在() -> None:
    """新的对外接口在 ``src/daemon/``。"""
    path = ROOT_DIR / "src" / "daemon" / "server.py"
    assert path.is_file(), "缺 src/daemon/server.py"
    source = path.read_text(encoding="utf-8")
    assert "http.server" in source
    assert "/api/pre-check" in source
    assert "/api/attach" in source


# ---------------------------------------------------------------------------
# 后端净化（但识别能力不能改坏）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative", BACKEND_FILES)
def test_后端不含私人化称呼(relative: str) -> None:
    """后端代码与数据里也不该有私人称呼。"""
    path = ROOT_DIR / relative
    if not path.is_file():
        pytest.skip("{} 已不存在（v5.0.0 搬走了）".format(relative))
    source = path.read_text(encoding="utf-8")
    for word in FORBIDDEN_PRIVATE:
        assert word not in source, "{} 里还有 {}".format(relative, word)


def test_功能性匹配标记没有被改坏() -> None:
    """⚠️ 这些字面量是**识别真实宿主进程**的判据。

    中性化文案的时候**绝对不能**把它们一起改掉 ——
    改了雷达就认不出宿主了。这是本文件最重要的一条断言。
    """
    source = _read("src/proc_tree.py")
    for marker in (
        '"n.e.k.o_plugin_agent_bridge"',
        '"n.e.k.o"',
        '"neko"',
        '"agent_bridge"',
    ):
        assert marker in source, "功能性标记丢了：{}".format(marker)


def test_上游出处标识保留() -> None:
    """出处标识要**保留** —— 合规靠的就是它。"""
    source = _read("src/agents/cloud_agents.json")
    assert "n.e.k.o_plugin_agent_bridge" in source


# ---------------------------------------------------------------------------
# 开源合规与致谢
# ---------------------------------------------------------------------------
def test_notice文件存在() -> None:
    """NOTICE 必须放在**仓库根目录**。"""
    path = ROOT_DIR / "NOTICE"
    assert path.is_file(), "根目录缺 NOTICE 文件"
    assert path.stat().st_size > 0


def test_notice写明了出处与协议() -> None:
    """任务书要求的那两句，必须一字不少地在。"""
    source = (ROOT_DIR / "NOTICE").read_text(encoding="utf-8")
    assert "addsas222/n.e.k.o_plugin_agent_bridge" in source
    assert "MIT" in source
    assert (
        "https://github.com/addsas222/n.e.k.o_plugin_agent_bridge" in source
    )
    assert "参考并使用了" in source


def test_readme有致谢段与链接() -> None:
    """README 底部要有致谢与仓库链接。"""
    source = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "## Acknowledgments" in source
    assert "addsas222/n.e.k.o_plugin_agent_bridge" in source
    assert "MIT" in source


def test_readme的致谢段指向notice() -> None:
    """致谢段要指向 NOTICE —— 两边说法得一致。"""
    source = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "NOTICE" in source


def test_notice里列出了具体复用了什么() -> None:
    """合规不只是写一句「参考了」—— 要说清楚复用了什么。"""
    source = (ROOT_DIR / "NOTICE").read_text(encoding="utf-8")
    for item in (
        "AgentSpec",
        "provider_manifest",
        "runner.py",
        "DEVNULL",
        "taskkill",
    ):
        assert item in source, item


def test_许可证是gpl3() -> None:
    """根目录的 LICENSE 必须是 **GPL-3.0 全文**。"""
    path = ROOT_DIR / "LICENSE"
    assert path.is_file(), "根目录缺 LICENSE 文件"
    source = path.read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in source
    assert "Version 3, 29 June 2007" in source
    assert "TERMS AND CONDITIONS" in source
    assert "END OF TERMS AND CONDITIONS" in source
    assert len(source) > 30_000, "LICENSE 像是被截断了"


def test_readme写了无头架构() -> None:
    """README 必须说清楚「现在没有界面了」。

    旧版 README 通篇在讲看板 / 卡片 / 设置页 ——
    那会让新用户以为双击会弹窗。v5.0.0 必须在开头就说清楚。
    """
    source = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "无头守护进程" in source
    assert "/api/pre-check" in source
    assert "/api/attach" in source
    assert "sentinel-daemon.exe" in source


# ---------------------------------------------------------------------------
# 全库零残留（v4.3.2 追加）
# ---------------------------------------------------------------------------
def test_全库没有旧称呼() -> None:
    """**全库零残留** —— 连历史文档与 CHANGELOG 都不许有。

    那个私人称呼被判定为「不宜出现在公开仓库的任何角落」，
    于是历史版本文档、CHANGELOG、资产盘点报告一并去个人化，
    资产盘点报告的文件名也一并改了。

    ⚠️ 这条断言查的是 **Git 正在追踪的文件** ——
    那才是真正会被公开的那一批。
    """
    word = FORBIDDEN_PRIVATE[0]
    hits: List[str] = []
    for path in _tracked_files():
        if not path.is_file() or path.stat().st_size > 4_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if word in text:
            hits.append(str(path.relative_to(ROOT_DIR)))
    assert not hits, "还有文件残留旧称呼：{}".format(", ".join(hits))


def test_版本号已经是501() -> None:
    """版本号必须同步到 5.0.1。

    ⚠️ v5.0.1 起改的是「强制单例锁下沉 + 两个同名进程归因」。
    """
    from src import __version__

    assert __version__ == "5.0.1"
