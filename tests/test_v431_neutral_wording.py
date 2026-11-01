"""v4.3.1 回归：界面文案中性化与开源合规致谢。

背景
====
v4.3.0 的雷达功能实机验证成功，但界面上的那个**特定上游项目的私人称呼**
过于私人化 —— 对「哨兵是一个通用工具」这件事没有好处。

v4.3.1 把这些文案中性化，并按 MIT 协议补上出处声明。

三条线必须同时成立
==================

1. **界面不能出现私人化 / 特定项目名** ——
   徽章、日志提示、Toast、设置页、示例配置、``--help``
   全部只能是中性词（「外部进程」「外部插件」）。

2. **识别能力不能被改坏** ——
   ``src/proc_tree.py`` 的 ``BRIDGE_HOST_*_MARKERS`` 里那些
   字面量（``n.e.k.o_plugin_agent_bridge`` / ``neko`` …）
   是**识别真实宿主进程的判据**，一个字都不能动。
   上游项目的出处标识也**要保留**（见 ``NOTICE``）。

3. **全库零残留**（v4.3.2 追加）——
   那个私人称呼后来被判定为「不宜出现在公开仓库的任何角落」，
   于是**历史文档、CHANGELOG、版本说明一并去个人化**。
   本文件最后一条断言盯的就是这件事。

本文件把这三条都钉住 —— 防止以后有人「顺手」改回去。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest

#: 仓库根目录。
ROOT_DIR = Path(__file__).resolve().parents[1]

#: 前端静态资源与模板 —— **用户直接看得见**的地方。
FRONTEND_FILES: Tuple[str, ...] = (
    "src/gui/static/dashboard.js",
    "src/gui/static/dashboard.css",
    "src/gui/static/router.js",
    "src/gui/static/settings_common.js",
    "src/gui/static/settings_index.js",
    "src/gui/static/settings_llm.js",
    "src/gui/static/settings_pipeline.js",
    "src/gui/static/settings_rules.js",
    "src/gui/static/settings_sessions.js",
    "src/gui/static/settings_system.js",
    "src/gui/static/settings_webhook.js",
    "src/gui/templates/dashboard.html",
    "src/gui/templates/settings_sessions.html",
)

#: 私人化的旧称呼 —— **一个字都不该再出现**。
#:
#: ⚠️ 这里用 **Unicode 转义**写，不直接写出来 ——
#: 本仓库要求**全库零残留**，连「禁止这个词」的常量本身
#: 都不能把它写进文件，否则全局搜索永远归不了零。
#: （第一个字 U+4E91，第二个字 U+6735）
FORBIDDEN_PRIVATE: Tuple[str, ...] = ("\u4e91\u6735",)

#: 旧标题的完整形态。
OLD_TITLE: str = FORBIDDEN_PRIVATE[0] + "派发的进程"

#: 旧的进程计数提示。
OLD_COUNT: str = "个" + FORBIDDEN_PRIVATE[0] + "派发进程。"

#: 特定项目名 —— 前端与用户可见文案里不许出现。
FORBIDDEN_PROJECT_NAMES: Tuple[str, ...] = (
    "n.e.k.o",
    "N.E.K.O",
    "neko",
    "agent_bridge",
    "addsas222",
)

#: 后端「源代码区」—— 注释与日志里不许出现私人称呼，
#: 但**允许**保留上游项目的出处标识（``NOTICE`` 指向它）。
BACKEND_FILES: Tuple[str, ...] = (
    "src/proc_tree.py",
    "src/procscan.py",
    "src/radar.py",
    "src/gui/server.py",
    "src/agents/__init__.py",
    "src/agents/launcher.py",
    "src/agents/cloud_agents.json",
    "src/adapters/text.py",
    "src/adapters/fingerprint.py",
)


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
    return [
        ROOT_DIR / item
        for item in out.stdout.split("\0")
        if item
    ]


# ---------------------------------------------------------------------------
# 任务一：界面文案中性化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative", FRONTEND_FILES)
def test_前端不含私人化称呼(relative: str) -> None:
    """前端一个旧称呼都不许有。"""
    source = _read(relative)
    for word in FORBIDDEN_PRIVATE:
        assert word not in source, "{} 里还有 {}".format(relative, word)


@pytest.mark.parametrize("relative", FRONTEND_FILES)
def test_前端不含特定项目名(relative: str) -> None:
    """前端不许出现特定上游项目的名字。"""
    source = _read(relative).lower()
    for word in FORBIDDEN_PROJECT_NAMES:
        assert word.lower() not in source, (
            "{} 里还有 {}".format(relative, word)
        )


def test_雷达面板标题是中性词() -> None:
    """旧标题 → 「检测到的外部进程」。"""
    source = _read("src/gui/static/dashboard.js")
    assert '"检测到的外部进程（"' in source
    assert OLD_TITLE not in source


def test_进程计数提示是中性词() -> None:
    """Toast 里的计数提示也要中性。"""
    source = _read("src/gui/static/dashboard.js")
    assert "个外部进程。" in source
    assert OLD_COUNT not in source


def test_疑似挂起的提示不含特定项目名() -> None:
    """tooltip 里那句「实测」不能指着某个特定项目。"""
    source = _read("src/gui/static/dashboard.js")
    assert "疑似挂起" in source
    block = source.split("badge.title =")[1][:400]
    for word in FORBIDDEN_PRIVATE + FORBIDDEN_PROJECT_NAMES:
        assert word.lower() not in block.lower(), word


def test_设置页的示例是中性词() -> None:
    """设置页 placeholder 里的示例名不该是某个具体项目。"""
    source = _read("src/gui/templates/settings_sessions.html")
    assert 'placeholder="neko"' not in source
    assert 'placeholder="my-cli"' in source


def test_示例配置里的示例是中性词() -> None:
    """示例配置会被用户直接抄走。"""
    source = _read("config/config.example.yaml")
    assert "neko" not in source.lower()
    assert "N.E.K.O" not in source


def test_exe帮助文本不含私人化称呼() -> None:
    """``--help`` 是用户直接看到的。"""
    source = _read("_构建与发布/scripts/sentinel_entry.py")
    for word in FORBIDDEN_PRIVATE:
        assert word not in source, word


def test_exe帮助文本披露致谢出处() -> None:
    """``--help`` 里**应该**出现上游出处。

    ⚠️ 这跟「不许出现特定项目名」是**两回事** ——
    「不许出现」指的是**把某个项目当成自己**的那种出现
    （面板标题、徽章、设置页示例）。
    而在致谢段落里**明示**出处，是 MIT 合规**要求**的，
    也是对上游最基本的尊重。
    """
    source = _read("_构建与发布/scripts/sentinel_entry.py")
    assert "addsas222/n.e.k.o_plugin_agent_bridge" in source
    assert "NOTICE" in source


# ---------------------------------------------------------------------------
# 任务二：后端净化（但识别能力不能改坏）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative", BACKEND_FILES)
def test_后端不含私人化称呼(relative: str) -> None:
    """后端代码与数据里也不该有私人称呼。"""
    source = _read(relative)
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
# 任务三：开源合规与致谢
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


# ---------------------------------------------------------------------------
# 任务四：全库零残留（v4.3.2 追加）
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


def test_本次版本说明用的是中性词() -> None:
    """v4.3.1 的版本说明里不许有旧称呼。

    ⚠️ 早先它有一处「改前 / 改后」对照表**正当引用**了旧称呼 ——
    去个人化之后，连那种引用也一并去掉了。
    """
    path = (
        ROOT_DIR
        / "_管理文档"
        / "文档与报告"
        / "versions"
        / "v4.3.1-neutral-wording-and-oss-attribution.md"
    )
    assert path.is_file(), "缺 v4.3.1 版本说明"
    source = path.read_text(encoding="utf-8")
    # 新措辞必须在。
    assert "检测到的外部进程" in source
    # 旧称呼一个字都不许有。
    for word in FORBIDDEN_PRIVATE:
        assert word not in source, word


def test_版本号已经是431() -> None:
    """版本号必须同步到 4.3.1。"""
    from src import __version__

    assert __version__ == "4.3.1"
