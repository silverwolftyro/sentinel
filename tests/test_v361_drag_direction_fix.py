"""v3.6.1 回归：中间上下拖拽条**方向不再反向**。

背景
====

v3.6.0 给中间面板加了「实时告警 ↔ 实时审核流水」的横向分隔条，
用户实测发现**方向完全反了** —— 鼠标往下拖，分隔条却往上跑。

根因**不是** ``deltaY`` 符号写反
================================

JS 里的映射 ``state.layout.audit = startAudit + dy`` 符号**一直是对的**
（往下拖 → ``dy > 0`` → 流水区变高）。真正的问题在**布局**：

* 中间面板的高度被 Grid 的 ``1fr`` **钉死**（``overflow: hidden``）；
* ``.live-audit`` 是面板的**最后一个** flex 子项且 ``flex: 0 0 auto``，
  它的**下沿被面板底边钉死**，高度 = ``--audit-h``；
* 分隔条紧贴在流水的**上沿**。

于是::

    分隔条 Y = 面板底边 − 分隔条高 − 流水高
    ⇒ d(分隔条 Y) = −d(流水高)

「流水长高 1px」必然把分隔条**顶上** 1px —— 正好与鼠标相反。
说白了：**把手挂在了那条「动不了」的边上**。

v3.6.1 的修法
=============

把 ``.live-audit`` 挪到分隔条**上方**，让它的**下沿**成为把手::

    分隔条 Y = 面板顶边 + 流水高
    ⇒ d(分隔条 Y) = +d(流水高)

把手这才**跟得上鼠标**，并且与左右两条分隔条
（会话栏的右边 / 日志面板的左边）**完全同构**：
都是「把手是那块板子的一条边，板子沿拖动方向长大」。

本文件把这件事**钉死**
======================

* DOM 顺序（流水 → 分隔条 → 告警标题 → 告警列表）；
* CSS 契约（流水区的分隔线在**下沿**、告警标题条锁高）；
* JS 契约（``+ dy`` 没被改回去、左右两条**一字未动**）；
* **几何模型** —— 把上面那套导数写成代码，
  以后谁再把流水区挪回分隔条下面，测试立刻红；
* 演示脚本契约 —— v3.6.0 的验收漏洞
  （只量「流水区盒子变高」，**没量把手自己的位移**）
  不许再回来。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]

#: 几何模型用的固定尺寸（像素）——
#: 具体数值不重要，重要的是**谁是伸缩段**。
MODEL_PANEL_HEIGHT = 742
MODEL_FLOW_HEIGHT = 200
MODEL_HEAD_HEIGHT = 40
MODEL_RESIZER_HEIGHT = 12


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _css_rule(css: str, selector: str) -> str:
    """取出 CSS 里某个选择器的第一条规则体（去注释）。

    :param css: 整份 CSS 文本。
    :param selector: 选择器（原文）。
    :return: 规则体；找不到返回空串。
    """
    pattern = re.compile(
        r"(?m)^{}\s*\{{([^}}]*)\}}".format(re.escape(selector))
    )
    match = pattern.search(css)
    if not match:
        return ""
    body = match.group(1)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return body


def _alert_panel_html() -> str:
    """取中间告警面板那一段 HTML。

    :return: ``<section class="panel panel--alerts">`` 到 ``</section>``。
    """
    html = _read("src", "gui", "templates", "dashboard.html")
    tail = html.split('class="panel panel--alerts"', 1)[1]
    return tail.split("</section>", 1)[0]


def _panel_children() -> list:
    """按 DOM 顺序列出中间面板的四个子元素。

    :return: 名字列表（``live-audit`` / ``resizer-audit`` /
        ``panel__head`` / ``panel__body``）。
    """
    block = _alert_panel_html()
    markers = (
        ("live-audit", 'class="live-audit"'),
        ("resizer-audit", 'id="resizer-audit"'),
        ("panel__head", 'class="panel__head"'),
        ("panel__body", 'class="panel__body"'),
    )
    found = []
    for name, marker in markers:
        index = block.find(marker)
        assert index >= 0, "中间面板里少了 {}".format(name)
        found.append((index, name))
    found.sort()
    return [name for _, name in found]


def _handle_y(order: list, flow_height: int = MODEL_FLOW_HEIGHT) -> int:
    """按 DOM 顺序算出中间分隔条的 Y 坐标（相对面板顶边）。

    ⚠️ 这就是 v3.6.1 的**根因模型**，不是随便编的数：

    * 面板高度固定（Grid 的 ``1fr`` + ``overflow: hidden``）；
    * 子元素自上而下依次排；
    * ``.panel__body`` 是**唯一**会伸缩的段（``flex: 1 1 auto``），
      它吃掉剩下的高度；其余段高度固定；
    * 所以「把手 Y」= 把手**前面**那些段的高度之和。

    :param order: 子元素顺序（名字列表）。
    :param flow_height: 流水区高度。
    :return: 把手 Y（像素）。
    """
    fixed = {
        "live-audit": flow_height,
        "resizer-audit": MODEL_RESIZER_HEIGHT,
        "panel__head": MODEL_HEAD_HEIGHT,
    }
    other = sum(fixed.get(name, 0) for name in order)
    y = 0
    for name in order:
        if name == "panel__body":
            y += MODEL_PANEL_HEIGHT - other
        elif name in fixed:
            y += fixed[name]
        if name == "resizer-audit":
            return y
    return y


# ---------------------------------------------------------------------------
# 一、DOM 顺序：把手必须挂在「能动的」那条边上
# ---------------------------------------------------------------------------
class TestDOMOrder:
    """中间面板的子元素顺序。"""

    def test_流水区在分隔条上方(self):
        """流水区必须在分隔条**上面** —— 它的下沿才是把手。"""
        order = _panel_children()
        assert order.index("live-audit") < order.index("resizer-audit")

    def test_告警标题条在分隔条下方(self):
        """告警标题条与告警列表在分隔条**下面**。"""
        order = _panel_children()
        assert order.index("resizer-audit") < order.index("panel__head")
        assert order.index("panel__head") < order.index("panel__body")

    def test_完整顺序(self):
        """四个子元素的顺序要一字不差。"""
        assert _panel_children() == [
            "live-audit",
            "resizer-audit",
            "panel__head",
            "panel__body",
        ]


# ---------------------------------------------------------------------------
# 二、CSS 契约
# ---------------------------------------------------------------------------
class TestCSSContract:
    """样式表必须配合新的 DOM 顺序。"""

    def test_流水区的分隔线在下沿(self):
        """流水区挪到上面后，分隔线得跟着换到 ``border-bottom``。"""
        block = _css_rule(
            _read("src", "gui", "static", "dashboard.css"), ".live-audit"
        )
        assert "border-bottom: 1px solid var(--border);" in block
        assert "border-top" not in block

    def test_告警标题条锁死高度(self):
        """标题条夹在分隔条下面，必须 ``flex: none`` —— 不然会被压扁。"""
        block = _css_rule(
            _read("src", "gui", "static", "dashboard.css"),
            ".panel--alerts .panel__head",
        )
        assert re.search(r"flex:\s*none", block)

    def test_流水区高度仍由变量驱动(self):
        """``--audit-h`` 的机制一字未改。"""
        css = _read("src", "gui", "static", "dashboard.css")
        block = _css_rule(css, ".live-audit")
        assert "var(--audit-h)" in block
        assert "max-height: 30vh" not in block
        alerts = _css_rule(css, ".panel--alerts")
        assert re.search(r"--audit-h:\s*200px", alerts)

    def test_窄屏的流水高度依旧压一档(self):
        """760px 断点继续把变量压到 140px。"""
        css = _read("src", "gui", "static", "dashboard.css")
        mobile = css.split("@media (max-width: 760px)", 1)[1]
        assert re.search(r"\.panel--alerts\s*\{[^}]*--audit-h:\s*140px", mobile)


# ---------------------------------------------------------------------------
# 三、JS 契约
# ---------------------------------------------------------------------------
class TestJSContract:
    """拖拽映射本身。"""

    def test_纵向映射仍然是加号(self):
        """``+ dy`` 的符号**没改** —— 修的是布局，不是符号。"""
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split('} else if (mode === "audit") {', 1)[1]
        block = block.split("}", 1)[0]
        assert "clampAudit(startAudit + dy)" in block
        assert "startAudit - dy" not in block

    def test_横向映射一字未动(self):
        """左右两条分隔条的映射不许被牵连。"""
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split("function bindResizer(", 1)[1]
        block = block.split("var stop = function", 1)[0]
        assert "clampSessions(startSessions + dx)" in block
        assert "clampStream(startStream - dx)" in block

    def test_告警区至少留一百二十像素(self):
        """夹紧上限要继续为告警区留出空间。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "LAYOUT_MIN_ALERT_AREA = 120;" in js
        block = js.split("function clampAudit(", 1)[1]
        block = block.split("\n  }", 1)[0]
        assert "LAYOUT_MIN_ALERT_AREA" in block


# ---------------------------------------------------------------------------
# 四、几何模型：根因的数学表达
# ---------------------------------------------------------------------------
class TestGeometryModel:
    """把手位移对流水高度的导数 —— 必须为 **+1**。"""

    def test_当前顺序下把手跟手(self):
        """流水长高 1px，把手必须**跟着**下移 1px。"""
        order = _panel_children()
        base = _handle_y(order, MODEL_FLOW_HEIGHT)
        grown = _handle_y(order, MODEL_FLOW_HEIGHT + 10)
        assert grown - base == 10

    def test_旧顺序会把把手顶上去(self):
        """把流水挪回分隔条**下面**，导数就变成 −1 —— 这正是 v3.6.0 的 Bug。"""
        old_order = [
            "panel__head",
            "panel__body",
            "resizer-audit",
            "live-audit",
        ]
        base = _handle_y(old_order, MODEL_FLOW_HEIGHT)
        grown = _handle_y(old_order, MODEL_FLOW_HEIGHT + 10)
        assert grown - base == -10

    def test_把手挂在流水下沿时流水不会顶到面板外(self):
        """流水区的下沿就是把手 —— 面板总高不变，两段此消彼长。"""
        order = _panel_children()
        assert order[0] == "live-audit"
        assert order[1] == "resizer-audit"


# ---------------------------------------------------------------------------
# 五、演示脚本契约：v3.6.0 的验收漏洞不许回来
# ---------------------------------------------------------------------------
class TestDemoContract:
    """演示脚本必须**量把手自己的位移**。"""

    def _demo(self) -> str:
        """读 v3.6.1 演示脚本。

        :return: 脚本文本。
        """
        return _read(
            "_构建与发布", "scripts", "demo_v361_drag_direction.py"
        )

    def test_演示脚本量了把手的位移(self):
        """v3.6.0 只量了流水区盒子的高度，漏掉了把手 —— 这里必须量。"""
        demo = self._demo()
        assert "resizer_audit" in demo
        assert "handle_follows_mouse_down" in demo
        assert "handle_follows_mouse_up" in demo

    def test_演示脚本两个方向都断言(self):
        """往下、往上都要断言 —— 只测一个方向等于没测。"""
        demo = self._demo()
        assert "flow_grows_on_drag_down" in demo
        assert "flow_shrinks_on_drag_up" in demo
        assert "alert_shrinks_on_drag_down" in demo
        assert "alert_grows_on_drag_up" in demo

    def test_演示脚本断言了横向行为不变(self):
        """左右两条分隔条也要一起验。"""
        demo = self._demo()
        assert "sessions_handle_follows" in demo
        assert "stream_handle_follows" in demo

    def test_旧的探针脚本不再保留(self):
        """排查用的探针不进版本库 —— 它只会让人以为那就是验收脚本。"""
        assert not (ROOT / "_构建与发布" / "scripts"
                    / "probe_v361_drag_direction.py").exists()
