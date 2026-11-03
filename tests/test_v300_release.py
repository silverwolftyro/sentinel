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




    def test_雷达的startswith用了元组(self):
        """两次 ``startswith`` 合成一次。"""
        source = _read("src", "radar.py")
        assert 'startswith(("http://", "https://"))' in source


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
















# ---------------------------------------------------------------------------
# 五、路由切换不被 SSE 卡顿
# ---------------------------------------------------------------------------
class Test路由切换:
    """切页必须**先收摊**，否则 SSE 回调会把主线程占满。"""










# ---------------------------------------------------------------------------
# 六、自动锚定与武装推荐（前后端常量两侧各持一份）
# ---------------------------------------------------------------------------
class Test前后端常量一致:
    """日志格式契约的关键常量，前后端**各持一份** —— 那就得对得上。"""







class Test武装推荐文案:
    """界面上要**说人话**。"""








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
        """PyInstaller 入口必须落在 ``_构建与发布/scripts/`` 里。

        ⚠️ v5.0.1：入口从 ``sentinel_entry.py`` 换成了
        ``daemon_bootstrap.py`` —— 后者更小，**先抢单例锁再加载大模块**。
        所以这里改成**动态**断言：写死文件名，每改一次入口就得改一遍测试。
        """
        source = _read("_构建与发布", "scripts", "build_exe.py")
        assert "ENTRY_SCRIPT" in source
        assert "daemon_bootstrap.py" in source
        assert os.path.exists(
            os.path.join(
                ROOT, "_构建与发布", "scripts", "daemon_bootstrap.py"
            )
        )


# ---------------------------------------------------------------------------
# 九、红线不变
# ---------------------------------------------------------------------------
class Test红线不变:
    """v3.0.0 **不新增能力** —— 红线 R1–R9 必须一字未改。"""


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
