"""v3.0.0 回归：智能化收尾与终极发布。

这一版**不新增能力模块**，只做四件事：

1. **全自动智能锚定与默认武装** —— 挂载会话自动提取目标、
   高置信度时**建议**武装（但绝不自动武装）；
2. **全局代码卫生** —— 严格静态检查零告警、异常一律留痕；
3. **前端移动端适配** —— 窄屏下看板与设置页都不横向溢出；
4. **终极文档与发布** —— 版本号四处一致、终端问候、白皮书。

本文件把这四件事里**能被机器验证的部分**钉住。
凡是「只能靠人看」的（白皮书读起来顺不顺），
不写假测试 —— 写不出有意义断言的，就不写。

⚠️ 本文件里有几条测试**读源码文本**。
这是有意的：那些约定（「先收摊再 fetch」「窄屏断点是 768px」）
一旦被改坏，**运行时**不一定报错，但用户立刻能感觉到卡顿 / 溢出。

⚠️ v3.9.0：窄屏断点由 760px 改成 **768px**（与设置页对齐），
窄屏按钮最小高度由 40px 改成 **44px** ——
这两条都是**有意提高**的契约，理由见本文件的 Test移动端适配。
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys

import pytest

#: 仓库根目录（本文件在 ``tests/`` 下）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _read(*parts: str) -> str:
    """读一个仓库里的文本文件。"""
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _read_js(name: str) -> str:
    """读前端脚本。"""
    return _read("src", "gui", "static", name)


def _read_css() -> str:
    """读看板样式表。"""
    return _read("src", "gui", "static", "dashboard.css")


def _read_settings_html() -> str:
    """读设置页**基座**模板。

    v3.0.2 把设置页拆成了「基座 + 5 个子页面」——
    布局、样式、窄屏媒体查询全在基座
    （``settings_base.html``）里，子页面只管自己的内容。
    """
    return _read("src", "gui", "templates", "settings_base.html")


def _python_files(relative_dir: str):
    """列出某个目录下的全部 ``.py`` 文件。"""
    base = os.path.join(ROOT, relative_dir)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if d not in ("__pycache__", ".git")
        ]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _parse(path: str):
    """把一个 Python 文件解析成 AST。"""
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


# ---------------------------------------------------------------------------
# 一、终端启动问候
# ---------------------------------------------------------------------------
class Test启动问候:
    """``Sentinel v3.0.0 is ready.`` —— 一行，说清楚「好了没」。"""

    def test_问候模板就是那一行(self):
        """模板必须是逐字固定的那一句。"""
        from src import console

        assert console.GREETING_MESSAGE == "Sentinel v{version} is ready."

    def test_问候按版本号渲染(self):
        """把版本号填进去就是完整的一句。"""
        from src import console

        assert (
            console.GREETING_MESSAGE.format(version="3.0.0")
            == "Sentinel v3.0.0 is ready."
        )

    def test_问候写进指定流(self):
        """可以指定输出流 —— 这样才测得了。"""
        from src import console

        stream = io.StringIO()
        console.print_greeting("3.0.0", stream=stream)

        assert stream.getvalue().strip() == "Sentinel v3.0.0 is ready."

    def test_问候不吞异常(self):
        """输出流坏了也不能把启动带崩。"""
        from src import console

        class _Broken:
            def write(self, _text):
                raise OSError("管道关了")

            def flush(self):
                raise OSError("管道关了")

        # 打印不出来是预期内的 —— 不该抛。
        console.print_greeting("3.0.0", stream=_Broken())

    def test_问候在公开接口里(self):
        """``__all__`` 要列出来 —— 它是本模块对外承诺的一部分。"""
        from src import console

        assert "print_greeting" in console.__all__
        assert "GREETING_MESSAGE" in console.__all__

    def test_版本一致(self):
        """问候里的版本必须和 ``src.__version__`` 一样。

        ⚠️ v3.0.1 起改成**动态**断言 —— 以前这里写死了 ``3.0.0``，
        发一次版就得改一遍，迟早会忘（v3.0.1 就真的忘了）。
        """
        import src
        from src import console

        assert (
            console.GREETING_MESSAGE.format(version=src.__version__)
            == "Sentinel v{} is ready.".format(src.__version__)
        )

    def test_就绪时才打问候(self):
        """端口**真的**能连上了才说「就绪」—— 不能提前吹。"""
        from src import console

        stream = io.StringIO()
        thread = console.announce_ready_when_up(
            "127.0.0.1",
            1,
            version="3.0.0",
            timeout=0.3,
            interval=0.05,
            open_browser=False,
        )
        thread.join(timeout=3)
        # 端口 1 连不上 → 不会走到 print_ready / print_greeting。
        assert "is ready" not in stream.getvalue()

    def test_没给版本就不打问候(self):
        """不传版本号时**不**打印 —— 宁可没有，也不能打错版本。"""
        from src import console

        assert "version" in console.announce_ready_when_up.__code__.co_varnames


# ---------------------------------------------------------------------------
# 二、静态检查零告警
# ---------------------------------------------------------------------------
class Test静态检查:
    """ruff 配置存在、可用、且**真的零告警**。"""

    def test_配置文件在根目录(self):
        """``ruff.toml`` 必须在仓库根目录。"""
        assert os.path.exists(os.path.join(ROOT, "ruff.toml"))

    def test_配置能解析(self):
        """配置本身不能有语法错误。"""
        import tomllib

        with open(os.path.join(ROOT, "ruff.toml"), "rb") as handle:
            data = tomllib.load(handle)

        assert data["line-length"] == 79
        assert "lint" in data

    def test_零告警(self):
        """整个仓库跑一遍 ruff —— 必须零告警。

        ⚠️ 这条测试会调一次子进程（几秒）。
        值得 —— 「零告警」是 v3.0.0 的**硬承诺**，
        有人偷偷加了一条 ``# noqa`` 或新代码带了告警，这里就会红。
        """
        try:
            result = subprocess.run(
                [sys.executable, "-m", "ruff", "check", ".", "--no-cache"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip("ruff 没装：{}".format(exc))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_惰性导入的豁免在配置里(self):
        """``PLC0415`` 必须关着 —— 惰性导入是**架构决定**，不是缺陷。"""
        import tomllib

        with open(os.path.join(ROOT, "ruff.toml"), "rb") as handle:
            data = tomllib.load(handle)

        assert "PLC0415" in data["lint"]["ignore"]

    def test_不强迫改typing风格(self):
        """``UP006`` / ``UP045`` 必须关着 —— 全项目统一用 ``typing.List``。"""
        import tomllib

        with open(os.path.join(ROOT, "ruff.toml"), "rb") as handle:
            data = tomllib.load(handle)

        ignore = data["lint"]["ignore"]
        assert "UP006" in ignore
        assert "UP045" in ignore

    def test_脚本与测试有各自豁免(self):
        """演示脚本可以 print，测试可以 assert。"""
        import tomllib

        with open(os.path.join(ROOT, "ruff.toml"), "rb") as handle:
            data = tomllib.load(handle)

        per_file = data["lint"]["per-file-ignores"]
        assert "T201" in per_file["src/console.py"]
        assert "S101" in per_file["tests/*.py"]


# ---------------------------------------------------------------------------
# 三、死代码清理的成果（钉住，别退回去）
# ---------------------------------------------------------------------------
class Test死代码清理:
    """v3.0.0 清掉的东西，不能悄悄长回来。"""

    def test_生产代码里没有吞异常的pass(self):
        """``except: pass`` 一个都不许剩。

        审查侧异常可以**降级**，但不能**消失** ——
        至少要留下一条能定位的日志（CONTRIBUTING §2.6）。
        """
        offenders = []
        for path in _python_files("src"):
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                body = [n for n in node.body if not isinstance(n, ast.Pass)]
                if not body:
                    offenders.append(
                        "{}:{}".format(
                            os.path.relpath(path, ROOT), node.lineno
                        )
                    )

        assert not offenders, "还有只 pass 的 except：" + ", ".join(offenders)

    def test_生产代码里没有裸except(self):
        """裸 ``except:`` 会把 ``KeyboardInterrupt`` 一起吞掉。"""
        offenders = []
        for path in _python_files("src"):
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    offenders.extend(
                        [
                            "{}:{}".format(
                                os.path.relpath(path, ROOT), node.lineno
                            )
                        ]
                    )

        assert not offenders, "还有裸 except：" + ", ".join(offenders)

    def test_未定义名的静态检查通过(self):
        """用 ruff 的 ``F821`` 专门查一遍「用了没定义的名字」。"""
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    ".",
                    "--no-cache",
                    "--select",
                    "F821",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip("ruff 没装：{}".format(exc))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_看板能解析类型注解(self):
        """``get_type_hints()`` 要能跑通 —— 这是 F821 的真实后果。"""
        import typing

        from src.gui import server

        hints = typing.get_type_hints(server.create_app)
        assert "watch_path" in hints

    def test_纳布尔检查用了math(self):
        """``ratio != ratio`` 是老写法 —— 换成 ``math.isnan`` 更清楚。"""
        source = _read("src", "gui", "layout_store.py")
        assert "math.isnan" in source
        assert "ratio != ratio" not in source

    def test_雷达的startswith用了元组(self):
        """两次 ``startswith`` 合成一次。"""
        source = _read("src", "radar.py")
        assert 'startswith(("http://", "https://"))' in source

    def test_目标来源标签是类变量(self):
        """可变类属性要标 ``ClassVar``。"""
        source = _read("src", "gui", "server.py")
        assert "GOAL_SOURCE_LABELS: ClassVar[Dict[str, str]]" in source

    def test_终端层有日志器(self):
        """``src/console.py`` 也要能留痕 —— 提示打不出来时是最后的消息。"""
        from src import console

        assert hasattr(console, "_LOGGER")


# ---------------------------------------------------------------------------
# 四、移动端适配
# ---------------------------------------------------------------------------
class Test移动端适配:
    """窄屏（≤768px）下看板与设置页都不能横向溢出。"""

    #: 窄屏断点 —— 两处必须**一致**，否则有一页会漏适配。
    #:
    #: ⚠️ v3.9.0：760px → **768px** ——
    #:    768 是手机竖屏的实际边界（iPhone 390pt / 安卓 360–412dp /
    #:    iPad mini 竖屏 744pt），取它才能把这些设备**全部**收进
    #:    移动端版式。设置页（templates/settings_base.html）
    #:    同一版改成 768px，两页依旧一致。
    #:
    #: 看板 CSS 里那个 760px 块**保留不动** —— 它是 v3.0.0 起的
    #: 既有契约，多个测试按文本断言它（test_v352 / test_v361）。
    #: 新的 768px 块排在它**后面**：媒体查询靠源码顺序决胜，
    #: 于是 ≤760 时以 768 块为准，761–768 时也只有 768 块命中。
    BREAKPOINT = "768px"

    def test_看板有窄屏媒体查询(self):
        css = _read_css()
        assert "@media (max-width: {})".format(self.BREAKPOINT) in css

    def test_看板窄屏用单列(self):
        """关键：``body`` 是 grid，隐式列的基线取子项 min-content。

        顶栏那行不换行的徽章会把整列撑到 584px ——
        必须显式写 ``minmax(0, 1fr)`` 把 min-content 的影响掐掉。
        """
        css = _read_css()
        assert "grid-template-columns: minmax(0, 1fr)" in css

    def test_看板窄屏按钮够大(self):
        """手机上按钮要按得中 —— 最小 **44px**。

        ⚠️ v3.9.0：40px → 44px ——
        44 像素是移动端公认的最小触控目标（Apple HIG 44pt /
        Material 48dp 的下限），40 像素实测偏小、容易误触。
        这是**有意提高**的契约。
        """
        css = _read_css()
        assert "min-height: 44px" in css

    def test_看板窄屏按钮不再低于44(self):
        """窄屏下**不得**残留 40px 的按钮规则 ——
        留着一条实际不生效的 40px 声明就是骗自己。
        """
        css = _read_css()
        assert "min-height: 40px" not in css

    def test_看板窄屏元信息允许换行(self):
        """长路径不能把页面撑破。"""
        css = _read_css()
        assert "overflow-wrap: anywhere" in css

    def test_看板窄屏计数器换行(self):
        """计数器用自适应网格，而不是固定三列。"""
        css = _read_css()
        assert "repeat(auto-fit, minmax(92px, 1fr))" in css

    def test_设置页有窄屏媒体查询(self):
        html = _read_settings_html()
        assert "@media (max-width: {})".format(self.BREAKPOINT) in html

    def test_设置页网格下限不硬(self):
        """``minmax(230px, 1fr)`` 的下限是**硬**的 —— 容器一窄就溢出。

        窄屏下必须换成 ``minmax(min(100%, 230px), 1fr)``。
        """
        html = _read_settings_html()
        assert "minmax(min(100%, 230px), 1fr)" in html

    def test_设置页输入框防自动放大(self):
        """iOS 上小于 16px 的输入框会**自动放大**，一放大就布局崩。"""
        html = _read_settings_html()
        assert "font-size: 16px" in html

    def test_设置页窄屏按钮换行(self):
        """按钮在窄屏下要能换行，不能挤成一团。"""
        html = _read_settings_html()
        assert ".settings-actions .btn" in html

    def test_断点两处一致(self):
        """看板与设置页的断点必须一样。"""
        css = _read_css()
        html = _read_settings_html()
        css_points = re.findall(r"max-width:\s*(\d+)px", css)
        html_points = re.findall(r"max-width:\s*(\d+)px", html)
        assert self.BREAKPOINT[:-2] in css_points
        assert self.BREAKPOINT[:-2] in html_points

    def test_窄屏块在文件末尾(self):
        """媒体查询要**最后**写 —— 前面写的规则会被它覆盖。

        所以它必须在文件靠后的位置（至少在 80% 之后）。
        """
        css = _read_css()
        index = css.index("@media (max-width: {})".format(self.BREAKPOINT))
        assert index > len(css) * 0.8

    def test_看板窄屏隐藏拖拽把手(self):
        """v3.9.0：≤768px 下所有分隔条与拖拽把手必须隐藏。

        手机上把手只有 8 像素宽，拖不动 —— 留着只会误导人。
        """
        css = _read_css()
        mobile = css[css.index("@media (max-width: 768px)"):]
        assert ".resizer" in mobile
        assert "display: none" in mobile

    def test_看板窄屏有底部Tab栏(self):
        """v3.9.0：移动端必须有底部 Tab 栏（桌面端默认隐藏）。"""
        css = _read_css()
        assert ".mobile-tabs" in css
        assert ".mobile-tabs__item" in css
        #: 桌面端默认不出现 —— 必须有一条不带媒体查询的 display:none。
        assert ".mobile-tabs {\n  display: none;\n}" in css


# ---------------------------------------------------------------------------
# 五、路由切换不被 SSE 卡顿
# ---------------------------------------------------------------------------
class Test路由切换:
    """切页必须**先收摊**，否则 SSE 回调会把主线程占满。"""

    def test_先teardown再fetch(self):
        """顺序不能反。"""
        js = _read_js("router.js")
        teardown_at = js.index("teardown();")
        fetch_at = js.index("fetch(url,")
        assert teardown_at < fetch_at

    def test_切页期间会置路由标记(self):
        """切页期间要在全局挂个标记。"""
        js = _read_js("router.js")
        assert "__SENTINEL_ROUTING__" in js

    def test_看板会丢弃在途事件(self):
        """标记为真时，事件处理器要**立刻返回**。"""
        js = _read_js("dashboard.js")
        assert "window.__SENTINEL_ROUTING__" in js

    def test_守卫在事件处理的最前面(self):
        """守卫必须在最前面 —— 放在中间就晚了。"""
        js = _read_js("dashboard.js")
        handler_at = js.index("function handleEvent(")
        guard_at = js.index("__SENTINEL_ROUTING__", handler_at)
        # 守卫必须在处理函数体内、且不在最后几百行（那是别处的引用）。
        assert guard_at - handler_at < 600

    def test_关闭事件流失败会留痕(self):
        """前端没有 logger，浏览器控制台就是它的日志。"""
        js = _read_js("dashboard.js")
        assert "关闭事件流失败" in js
        assert "console.error" in js

    def test_事件解析失败会留痕(self):
        """解析失败不能静默 —— 否则「日志不更新」无从查起。"""
        js = _read_js("dashboard.js")
        assert "事件流数据解析失败" in js

    def test_路由器暴露teardown(self):
        """``teardown`` 要能被外部调用。"""
        js = _read_js("router.js")
        assert "teardown: teardown" in js

    def test_脚本语法正确(self):
        """``node --check`` —— 没装 node 就跳过。"""
        names = (
            "dashboard.js",
            "router.js",
            "settings_common.js",
            "settings_index.js",
            "settings_llm.js",
            "settings_webhook.js",
            "settings_rules.js",
            "settings_sessions.js",
        )
        for name in names:
            path = os.path.join(ROOT, "src", "gui", "static", name)
            try:
                result = subprocess.run(
                    ["node", "--check", path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                pytest.skip("node 没装")
            assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 六、自动锚定与武装推荐（前后端常量两侧各持一份）
# ---------------------------------------------------------------------------
class Test前后端常量一致:
    """日志格式契约的关键常量，前后端**各持一份** —— 那就得对得上。"""

    def test_武装建议置信度后端(self):
        from src.gui import server

        assert server.ARM_SUGGEST_CONFIDENCE == 0.95

    def test_武装建议置信度前端(self):
        js = _read_js("dashboard.js")
        match = re.search(r"ARM_SUGGEST_CONFIDENCE\s*=\s*([0-9.]+)", js)
        assert match is not None
        assert float(match.group(1)) == 0.95

    def test_两侧一致(self):
        from src.gui import server

        js = _read_js("dashboard.js")
        match = re.search(r"ARM_SUGGEST_CONFIDENCE\s*=\s*([0-9.]+)", js)
        assert float(match.group(1)) == server.ARM_SUGGEST_CONFIDENCE

    def test_自动提取只扫开头(self):
        """用户第一句话一定在开头 —— 扫全文件是浪费。"""
        from src.gui import server

        assert server.INITIAL_GOAL_SCAN_LINES == 200

    def test_自动提取有长度上限(self):
        """目标是**锚点**，不是全文存档。"""
        from src.gui import server

        assert server.INITIAL_GOAL_MAX_CHARS == 200


class Test武装推荐文案:
    """界面上要**说人话**。"""

    def test_有建议武装字样(self):
        js = _read_js("dashboard.js")
        assert "建议武装" in js

    def test_有高置信度气泡(self):
        js = _read_js("dashboard.js")
        assert "高置信度匹配" in js
        assert "建议开启守护者" in js

    def test_有自动提取标记(self):
        js = _read_js("dashboard.js")
        assert "[自动提取]" in js

    def test_气泡元素有样式(self):
        css = _read_css()
        assert ".session-item__hint" in css

    def test_自动提取标记有样式(self):
        css = _read_css()
        assert ".session-item__auto" in css

    def test_建议态有脉冲动画(self):
        """高亮不能只是换个颜色 —— 得让人一眼看见。"""
        css = _read_css()
        assert 'data-suggest="true"' in css
        assert "@keyframes arm-suggest" in css

    def test_绝不自动武装(self):
        """建议 ≠ 自动开。"""
        js = _read_js("dashboard.js")
        # 武装只能由用户点击触发（toggleArm）。
        assert "toggleArm" in js
        # 初始化路径里不能有「自动调用 toggleArm」。
        assert "session.armed = true" not in js

    def test_代码适配器认得用户消息(self):
        """三种 Agent 都要能提取出首条用户消息。"""
        from src.adapters import claude, codex, mcode

        for module in (codex, mcode, claude):
            source = open(module.__file__, encoding="utf-8").read()
            assert "user_message" in source, module.__name__


# ---------------------------------------------------------------------------
# 七、版本一致性（四处）
# ---------------------------------------------------------------------------
class Test版本一致:
    """版本号**四处**必须一样：包、CHANGELOG、版本文档、Git 标签。

    ⚠️ v3.0.1 起全部改成**动态**断言 ——
    期望值一律从 ``src.__version__`` 取。
    以前这里是写死的版本号，每发一次版就得改一遍，
    而「忘了改」正是版本号不同步的**头号原因**。
    """

    def test_包版本(self):
        """版本号必须是一个合法的语义化版本号。"""
        import re

        import src

        assert re.match(r"^\d+\.\d+\.\d+$", src.__version__)

    def test_变更日志有当前版本(self):
        """CHANGELOG 里必须有当前版本的条目。"""
        import src

        changelog = _read("CHANGELOG.md")
        assert "[v{}]".format(src.__version__) in changelog

    def test_有版本文档(self):
        """``versions/`` 里必须有当前版本的说明文档。"""
        import src

        versions = os.path.join(ROOT, "_管理文档", "文档与报告", "versions")
        names = os.listdir(versions)
        assert any(
            name.startswith("v{}".format(src.__version__)) for name in names
        )

    def test_打包入口回退值(self):
        """exe 解包顺序异常时的兜底版本 —— 也要跟当前版本一样。"""
        import src

        source = _read("_构建与发布", "scripts", "sentinel_entry.py")
        assert '__version__ = "{}"'.format(src.__version__) in source

    def test_一致性脚本通过(self):
        """跑一遍自检脚本。"""
        script = os.path.join(
            ROOT, "_构建与发布", "scripts", "check_version_consistency.py"
        )
        result = subprocess.run(
            [sys.executable, script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_命令行帮助里有新特性(self):
        """``--help`` 里要能看到当前版本带来了什么。"""
        import src

        source = _read("_构建与发布", "scripts", "sentinel_entry.py")
        assert "v{} 新增".format(src.__version__) in source

    def test_命令行帮助里有问候语(self):
        """问候语里的版本号要用占位符 —— 否则每次发版都得手改一遍。"""
        source = _read("_构建与发布", "scripts", "sentinel_entry.py")
        assert "Sentinel v{version} is ready." in source


# ---------------------------------------------------------------------------
# 八、打包
# ---------------------------------------------------------------------------
class Test打包:
    """打包版要把**惰性导入**的模块显式列进去。"""

    def test_打包脚本在指定目录(self):
        """PyInstaller 入口固定在 ``_构建与发布/scripts/``。"""
        assert os.path.exists(
            os.path.join(
                ROOT, "_构建与发布", "scripts", "build_exe.py"
            )
        )

    def test_隐藏导入列了procscan(self):
        """``src.procscan`` 是函数内惰性导入的 —— 不列就会被裁掉。"""
        source = _read("_构建与发布", "scripts", "build_exe.py")
        assert "src.procscan" in source

    def test_psutil是可选依赖(self):
        """psutil 装了才点名 —— 没装不该有警告。"""
        source = _read("_构建与发布", "scripts", "build_exe.py")
        assert "OPTIONAL_HIDDEN_IMPORTS" in source
        assert "psutil" in source

    def test_入口脚本在脚本目录(self):
        source = _read("_构建与发布", "scripts", "build_exe.py")
        assert "sentinel_entry.py" in source


# ---------------------------------------------------------------------------
# 九、红线不变
# ---------------------------------------------------------------------------
class Test红线不变:
    """v3.0.0 **不新增能力** —— 红线 R1–R9 必须一字未改。"""

    def test_应用层不认识引擎(self):
        """GUI 应用层**不许在模块顶层** import ``src.core.engine``。

        ⚠️ 允许**函数内**惰性导入 —— 那是本项目刻意的架构决定
        （ARCHITECTURE.md：GUI 通过**回调注入**拿到引擎能力，
        引擎模块只在使用时才拉进来）。
        """
        path = os.path.join(ROOT, "src", "gui", "server.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            assert "core.engine" not in module, module
            assert "core" not in module, module

    def test_版本文档声明红线未改(self):
        """版本文档必须**如实**写明红线没动过。"""
        versions = os.path.join(ROOT, "_管理文档", "文档与报告", "versions")
        names = [
            name for name in os.listdir(versions)
            if name.startswith("v3.0.0")
        ]
        assert names
        with open(
            os.path.join(versions, names[0]), encoding="utf-8"
        ) as handle:
            text = handle.read()
        assert "红线" in text
        assert "一字未改" in text

    def test_变更日志声明红线未改(self):
        changelog = _read("CHANGELOG.md")
        assert "一字未改" in changelog

    def test_没有新增动作类能力(self):
        """v3.0.0 不该出现新的「动作」按钮 —— 动作都要用户点头。

        已知的三类动作：注入 / 回滚 / 复活。
        """
        js = _read_js("dashboard.js")
        for known in ("inject", "rollback", "resume"):
            assert known in js
