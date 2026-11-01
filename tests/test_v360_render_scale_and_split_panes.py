"""v3.6.0 回归：日志渲染档位 · 多面板拖拽折叠 · 路径校验 · 问答高亮。

背景
====

v3.5.2 的内核已经到位，但**前端呈现**还有四个「致命」痛点：

1. 雷达空态摆出的那条命令指向一个**根本不存在**的绝对路径 ——
   用户照抄、点【接入】，换回来的是一句冷冰冰的 400；
2. 右侧「被监控日志」全是**原始 JSON**，一行几百个字符没法读；
3. 布局是**写死**的 Grid —— 想多看告警还是多看日志，由不得用户；
4. 流水区里用户提问与 Agent 回答跟普通流水**长得一模一样**，
   一眼看不出哪句才是「核心上下文」。

本文件把这四件事**钉住**，一条都不许退化。

与 v3.5.1 的区别
==============

* 前端的「档位解析」用 Node 的 ``vm`` 沙箱**真的跑一遍**
  （``tests/fixtures/v360_frontend_harness.js``），
  断言的是**行为**而不是文本；
* 拖拽布局验的是**契约**（元素在不在、宽度由不由 CSS 变量驱动）——
  真实拖拽效果要靠指针事件，交给演示脚本截图取证；
* 后端的路径校验端点与 ``extract_snippet`` 都是**纯 Python**，
  可以直接断言输出。

⚠️ Node 不在时那一条**跳过**而不是失败 ——
   Node 不是产品的运行依赖，不能因为它缺席就红。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.core import live_audit
from src.core.live_audit import extract_snippet
from src.gui.server import create_app

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]

#: 任务书里那句一字不差的路径不存在提示语。
PATH_MISSING_HINT = "文件不存在，请检查路径或重新生成"

#: 任务书里那句一字不差的空态提示语。
RADAR_MISSING_HINT = "请先运行该命令，生成文件后再点击接入。"


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _make_app(tmp_path, **kwargs):
    """造一个指向临时目录的应用（默认不启动后台线程）。

    :param tmp_path: pytest 的临时目录。
    :param kwargs: 透传给 :func:`create_app` 的参数。
    :return: ASGI 应用。
    """
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")
    return create_app(
        str(watch),
        str(alert_log),
        start_watcher=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 一、后端：路径校验端点
# ---------------------------------------------------------------------------
class TestPathExistsEndpoint:
    """``GET /api/path_exists`` —— 接入前先问一句「文件在不在」。"""

    def test_存在的文件报存在(self, tmp_path):
        """存在的文件 → ``exists`` 为真。"""
        target = tmp_path / "session.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get(
                "/api/path_exists", params={"path": str(target)}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["exists"] is True
        assert data["is_file"] is True

    def test_不存在的文件报不存在(self, tmp_path):
        """不存在的文件 → ``exists`` 为假，**不报错**。"""
        missing = tmp_path / "根本不存在.jsonl"
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get(
                "/api/path_exists", params={"path": str(missing)}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["exists"] is False
        assert data["is_file"] is False

    def test_目录不是文件(self, tmp_path):
        """目录存在，但不是文件 → ``exists`` 真、``is_file`` 假。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get(
                "/api/path_exists", params={"path": str(tmp_path)}
            )

        data = response.json()
        assert data["exists"] is True
        assert data["is_file"] is False

    def test_空路径不崩(self, tmp_path):
        """空路径 → 报不存在，而不是 500。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get("/api/path_exists", params={"path": ""})

        assert response.status_code == 200
        assert response.json()["exists"] is False

    def test_相对路径按当前工作目录解析(self, tmp_path):
        """相对路径也要能用 —— 前端给的是用户自己终端里的相对路径。"""
        app = _make_app(tmp_path)
        with TestClient(app) as client:
            response = client.get(
                "/api/path_exists", params={"path": "logs/capture.jsonl"}
            )

        assert response.status_code == 200
        data = response.json()
        assert "path" in data
        assert data["path"]


# ---------------------------------------------------------------------------
# 二、后端：流水区必须能引用到「用户提问」的正文
# ---------------------------------------------------------------------------
class TestExtractSnippetForUserMessage:
    """``extract_snippet`` 必须覆盖 ``user_message``。"""

    def test_用户提问取正文(self):
        """user_message → 提问正文（不然问答气泡里是空的）。"""
        snippet = extract_snippet({
            "type": "user_message",
            "text": "帮我看看鉴权中间件为什么放行",
        })
        assert snippet == "帮我看看鉴权中间件为什么放行"

    def test_用户提问按四十字截断(self):
        """太长 → 截到上限（含省略号）。"""
        snippet = extract_snippet({
            "type": "user_message",
            "text": "问" * 200,
        })
        assert len(snippet) == 40
        assert snippet.endswith("…")

    def test_坏事件不崩(self):
        """非法输入 → 空串（架构红线 R5：审查侧异常一律降级）。"""
        assert extract_snippet(None) == ""
        assert extract_snippet({"type": "user_message"}) == ""

    def test_先挖噪声再截断(self):
        """框架噪声段必须**在截断之前**被挖掉。

        ⚠️ 这是实机踩出来的坑：
           codex 把 ``<environment_context>`` 塞进用户消息，
           而流水气泡只留 40 字 —— 先截断的话，
           ``</environment_context>`` 这个闭合标签先被切没，
           前端再挖就只剩个光秃秃的 ``<cwd>``，
           气泡里显示的既不是用户问的，也不是完整提示。

        :return: None。
        """
        snippet = extract_snippet({
            "type": "user_message",
            "text": (
                "<environment_context>\n"
                "<cwd>D:\\项目\\<项目目录></cwd>\n"
                "</environment_context>\n"
                "刚才那个问题还没解决，请继续处理。"
            ),
        })
        assert snippet == "刚才那个问题还没解决，请继续处理。"
        assert "<cwd>" not in snippet
        assert "environment_context" not in snippet

    def test_噪声挖掉后正文变长也不截(self):
        """噪声占了大半篇幅时，真实提问依然要**完整**留下。

        :return: None。
        """
        snippet = extract_snippet({
            "type": "user_message",
            "text": (
                "<skills_instructions>\n" + "规则说明。" * 30 + "\n"
                "</skills_instructions>\n"
                "请把鉴权中间件补上单元测试。"
            ),
        })
        assert snippet == "请把鉴权中间件补上单元测试。"

    def test_噪声标签清单与前端一致(self):
        """后端挖的标签必须跟前端挖的**一模一样**。

        ⚠️ 两边不一致就会出现「日志面板干净、流水气泡全是标签」的分裂。

        :return: None。
        """
        js = _read("src", "gui", "static", "dashboard.js")
        start = js.index("var LOG_NOISE_TAGS = [")
        block = js[start:js.index("];", start)]
        frontend = set(re.findall(r'"([^"]+)"', block))
        backend = set(live_audit._NOISE_TAGS)
        assert frontend == backend


# ---------------------------------------------------------------------------
# 三、前端契约：布局（拖拽 + 折叠）
# ---------------------------------------------------------------------------
class TestLayoutContract:
    """面板布局必须由**真的 DOM** + **CSS 变量**驱动。"""

    def test_模板里有三个分隔条(self):
        """左 / 中 / 右三条分隔条都得在 DOM 里。

        ⚠️ 必须是**真的元素** —— 伪元素拿不到指针事件，拖不动。
        """
        html = _read("src", "gui", "templates", "dashboard.html")
        assert 'id="resizer-sessions"' in html
        assert 'id="resizer-audit"' in html
        assert 'id="resizer-stream"' in html
        assert html.count('role="separator"') == 3

    def test_模板里有折叠按钮与窄条(self):
        """会话栏的收起按钮与折叠后的窄条都要在。"""
        html = _read("src", "gui", "templates", "dashboard.html")
        assert 'id="sessions-collapse"' in html
        assert 'id="sessions-rail"' in html
        assert 'id="sessions-expand"' in html
        assert 'id="sessions-rail-count"' in html

    def test_主体容器有布局锚点(self):
        """``.body`` 要有 id —— JS 要往它身上写 CSS 变量。"""
        html = _read("src", "gui", "templates", "dashboard.html")
        assert 'id="body-layout"' in html

    def test_网格列由CSS变量驱动(self):
        """五列网格，宽度全部来自变量 —— 排版规则只有 CSS 一份解释者。"""
        css = _read("src", "gui", "static", "dashboard.css")
        block = css.split(".body {", 1)[1].split("}", 1)[0]
        assert "grid-template-columns:" in block
        assert "var(--sessions-w)" in block
        assert "var(--stream-w)" in block
        assert "var(--resizer-w)" in block

    def test_宽度变量有默认值(self):
        """三根变量都要有默认值 —— 否则页面第一帧会散架。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert "--sessions-w:" in css
        assert "--stream-w:" in css
        assert "--resizer-w:" in css
        assert "--audit-h:" in css

    def test_折叠态有专门的类(self):
        """收起时靠一个类切换，而不是 JS 到处改样式。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert ".body.is-sessions-collapsed {" in css
        assert ".sessions__rail {" in css

    def test_中间面板不再有绝对定位叠压(self):
        """v3.5.2 的教训：流水区不能回到绝对定位压住告警卡片。"""
        css = _read("src", "gui", "static", "dashboard.css")
        block = css.split(".live-audit {", 1)[1].split("}", 1)[0]
        assert "position: absolute" not in block
        assert "z-index" not in block

    def test_流水区高度由变量驱动且没有死上限(self):
        """流水区高度由 ``--audit-h`` 决定 —— 30vh 的死上限会截断用户拖的比例。"""
        css = _read("src", "gui", "static", "dashboard.css")
        block = css.split(".live-audit {", 1)[1].split("}", 1)[0]
        assert "var(--audit-h)" in block
        assert "30vh" not in block

    def test_折叠态会摘掉内联宽度(self):
        """收起时必须摘掉内联的 ``--sessions-w``。

        ⚠️ 这是 v3.6.0 实机演示抓出来的**真 bug**：
           拖拽之后 ``applyLayout()`` 会把宽度写成**内联**样式，
           而内联的优先级**高于**样式表里的折叠规则
           （``.body.is-sessions-collapsed { --sessions-w: 46px }``）——
           不摘掉的话，左边会留下一大块空白，会话栏**压根收不起来**。
        """
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split("function applyLayout(", 1)[1].split("\n  }", 1)[0]
        assert "state.layout.collapsed" in block
        assert 'setCssVar(layout, "--sessions-w", null)' in block

    def test_收起动作会重新套用布局(self):
        """``setSessionsCollapsed()`` 必须重跑一次 ``applyLayout()``。

        只有它会去摘那枚内联变量 ——
        光切个 class 是**摘不掉**内联样式的。
        """
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split("function setSessionsCollapsed(", 1)[1]
        block = block.split("\n  }", 1)[0]
        assert "applyLayout(false)" in block
        assert 'classList.toggle("is-sessions-collapsed"' in block

    def test_分隔条有可见样式(self):
        """分隔条得看得见 —— 不然用户不知道能拖。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert ".resizer {" in css
        assert ".resizer--v {" in css
        assert ".resizer--h {" in css
        assert ".resizer:hover::after" in css

    def test_折叠时左分隔条必须留在网格里(self):
        """收起会话栏**不能**把左分隔条从网格流里摘掉。

        ⚠️ 这是 v3.6.0 实机演示抓出来的**第二个真 bug**：
           ``display: none`` 会让 ``#resizer-sessions`` **退出网格流**，
           后面三个网格项自动往前补位 ——
           「实时告警」被塞进 8px 的分隔条列、
           「被监控日志」也被塞进 8px，
           中间那条分隔条反倒霸占了整整一列 1fr。
           结果就是：收起源话栏之后，**整页只剩一条空窄条**。

           所以折叠态必须用 ``visibility: hidden`` ——
           保留它的**网格占位**，列宽不变、其它面板纹丝不动。

        :return: None。
        """
        css = _read("src", "gui", "static", "dashboard.css")
        marker = ".body.is-sessions-collapsed #resizer-sessions {"
        assert marker in css
        block = css.split(marker, 1)[1].split("}", 1)[0]
        assert "display: none" not in block
        assert "visibility: hidden" in block

    def test_窄屏隐藏分隔条(self):
        """单列堆叠下拖拽没有意义 —— 必须藏起来。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert "@media (max-width: 980px)" in css
        block = css.split("@media (max-width: 980px)", 1)[1]
        assert ".resizer {" in block


# ---------------------------------------------------------------------------
# 四、前端契约：日志渲染档位
# ---------------------------------------------------------------------------
class TestLogScaleContract:
    """无极滑条与三档行为必须在代码里落地。

    ⚠️ v3.7.0 起档位滑条**整条移出主看板** ——
    档位改由「设置 › 流水线控制 › 呈现与摘要」统一配置，
    服务端下发，所有窗口共享同一份。
    这里只钉住「看板不再有滑条、设置页有滑条」这条**新契约**。
    """

    def test_看板不再有滑条(self):
        """⚠️ v3.6.0 的滑条必须**真的删掉**，不能只是藏起来。"""
        html = _read("src", "gui", "templates", "dashboard.html")
        assert 'id="log-scale"' not in html
        assert 'id="log-scale-value"' not in html
        assert "log-scale__slider" not in html

    def test_滑条搬到设置页(self):
        """档位滑条换了个家 —— 在「呈现与摘要」卡片里。"""
        html = _read("src", "gui", "templates", "settings_pipeline.html")
        assert 'id="presentation-log-compression"' in html
        assert 'id="presentation-audit-compression"' in html
        assert 'min="0"' in html
        assert 'max="100"' in html

    def test_三档边界写在代码里(self):
        """30 / 70 两条分界线。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "var LOG_SCALE_CODE_MAX = 30;" in js
        assert "var LOG_SCALE_SUMMARY_MIN = 70;" in js

    def test_档位标签有三种(self):
        """纯代码 / 自然语言 / 高度摘要（v3.7.0 多了「极限摘要」）。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert '"纯代码"' in js
        assert '"自然语言"' in js
        assert '"高度摘要"' in js
        assert '"极限摘要"' in js

    def test_原始行有缓存(self):
        """重画要靠缓存 —— 重新拉后端会闪。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "logRaw: []" in js
        assert "function redrawLogLines(" in js

    def test_内容比对用原始行而不是DOM(self):
        """⚠️ 必须拿原始行比 —— 自然语言模式下 DOM 文字根本不是原始行。

        拿 DOM 比会导致每 15 秒重画一次，冲掉用户的阅读位置
        （v3.5.1 修过的坑，不能重犯）。
        """
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split("function logContentUnchanged", 1)[1]
        block = block.split("\n  }", 1)[0]
        assert "state.logRaw" in block
        assert "querySelectorAll" not in block

    def test_档位不再存本地存储(self):
        """⚠️ v3.7.0：档位**不再**存 localStorage ——
        它改成服务端配置，多个窗口必须看到同一份，
        存本地就会出现「这个窗口改了、那个窗口没改」的鬼故事。

        布局比例仍然存本地 —— 那是**每个窗口自己的**手感。
        """
        js = _read("src", "gui", "static", "dashboard.js")
        assert 'LOG_SCALE_STORAGE_KEY = "sentinel.logScale.v1"' not in js
        assert 'LAYOUT_STORAGE_KEY = "sentinel.layout.v1"' in js
        # 档位只能从服务端读。
        assert '"/api/presentation"' in js

    def test_JSON高亮有五种颜色(self):
        """键 / 字符串 / 数字 / 布尔 / 标点。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert ".log-json__key {" in css
        assert ".log-json__string {" in css
        assert ".log-json__number {" in css
        assert ".log-json__bool {" in css
        assert ".log-json__punct {" in css

    def test_不用innerHTML(self):
        """日志内容是**外部输入** —— 拼字符串进 HTML 等于开门揖盗。"""
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split("function renderLogJson(", 1)[1]
        block = block.split("\n  }", 1)[0]
        assert "innerHTML" not in block


# ---------------------------------------------------------------------------
# 五、前端契约：路径校验与空态
# ---------------------------------------------------------------------------
class TestPathValidationContract:
    """接入前校验与空态提示必须在前端落地。"""

    def test_前端会先问后端文件在不在(self):
        """点【接入】先打 ``/api/path_exists``。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert '"/api/path_exists?path="' in js
        assert "function verifyPathThenAttach(" in js

    def test_红字提示逐字一致(self):
        """任务书原文：「文件不存在，请检查路径或重新生成」。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert PATH_MISSING_HINT in js
        css = _read("src", "gui", "static", "dashboard.css")
        assert ".attach__error {" in css

    def test_校验失败不阻断用户(self):
        """校验接口自己出毛病时退回原流程 —— 不能把用户堵死。"""
        js = _read("src", "gui", "static", "dashboard.js")
        block = js.split("function verifyPathThenAttach(", 1)[1]
        block = block.split("\n    manualButton.addEventListener", 1)[0]
        assert "改用直接接入" in block
        assert "doAttach(target)" in block

    def test_空态缺文件时用相对路径(self):
        """文件没生成时只给相对路径，不给不存在的绝对路径。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert 'RADAR_EMPTY_RELATIVE_PATH = "logs/capture.jsonl"' in js
        assert RADAR_MISSING_HINT in js

    def test_空态缓存键带上存在性(self):
        """文件从「没有」变成「有」时，提示语也得跟着换。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert 'setAttribute("data-ready"' in js
        assert 'getAttribute("data-ready")' in js

    def test_后端会下发捕获文件是否存在(self):
        """``/api/process_activity`` 要带 ``capture_exists``。"""
        source = _read("src", "gui", "server.py")
        assert '"capture_exists"' in source


# ---------------------------------------------------------------------------
# 六、前端契约：流水区的问答高亮
# ---------------------------------------------------------------------------
class TestFlowHighlightContract:
    """用户提问与 Agent 回答要能被一眼认出来。"""

    def test_流水认得出用户提问(self):
        """``user_message`` 必须有**自己的**分支。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "🧑 用户提问：" in js
        assert 'type === "user_message"' in js

    def test_回答用统一口径(self):
        """与日志面板口径统一：「🤖 Agent 回答：」。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "🤖 Agent 回答：" in js

    def test_问答有气泡样式(self):
        """气泡样式：左边条 + 底色，不是抢文字颜色。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert ".live-audit__line--user," in css
        assert ".live-audit__line--reply {" in css

    def test_问答会挂上修饰类(self):
        """渲染时要真的加上 ``--user`` / ``--reply``。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "function auditEntryKind(" in js
        assert 'line.classList.add("live-audit__line--" + kind)' in js


# ---------------------------------------------------------------------------
# 七、前端逻辑：**真的跑一遍**（Node 沙箱）
# ---------------------------------------------------------------------------
class TestFrontendBehaviour:
    """用 Node 把 dashboard.js 跑起来，断言它的**行为**。"""

    def test_v360前端行为(self):
        """Node 在 → 跑夹具；不在 → 跳过（Node 不是运行依赖）。"""
        node = shutil.which("node")
        if not node:
            pytest.skip("本机没有 Node，跳过前端行为测试")
        harness = ROOT / "tests" / "fixtures" / "v360_frontend_harness.js"
        result = subprocess.run(
            [node, str(harness)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            # Node 输出是 UTF-8，而本机默认编码是 GBK，必须显式指定，
            # 否则中文用例名会让解码线程抛 UnicodeDecodeError。
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FAIL" not in result.stdout
        assert "通过" in result.stdout

    def test_v351前端行为没有退化(self):
        """v3.5.1 的用例也必须全过 —— 新功能不许把旧契约顶掉。"""
        node = shutil.which("node")
        if not node:
            pytest.skip("本机没有 Node，跳过前端行为测试")
        harness = ROOT / "tests" / "fixtures" / "v351_frontend_harness.js"
        result = subprocess.run(
            [node, str(harness)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FAIL" not in result.stdout


# ---------------------------------------------------------------------------
# 八、版本口径
# ---------------------------------------------------------------------------
def test_版本号跟当前包版本一致():
    """版本号必须跟当前包版本一致 —— 这里钉住其中一处。

    ⚠️ **动态**断言 —— 写死版本号正是版本不同步的头号原因
    （v3.0.1 起 ``test_v300_release.py`` 就已经全面动态化）。
    """
    import src

    source = _read("src", "__init__.py")
    assert '"{}"'.format(src.__version__) in source
    assert (ROOT / "CHANGELOG.md").exists()
    assert json.dumps(src.__version__)[1:-1] in _read(
        "CHANGELOG.md"
    ) or src.__version__ in _read("CHANGELOG.md")
