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
from pathlib import Path

from src.core.live_audit import extract_snippet

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




# ---------------------------------------------------------------------------
# 一、后端：路径校验端点
# ---------------------------------------------------------------------------
class TestPathExistsEndpoint:
    """``GET /api/path_exists`` —— 接入前先问一句「文件在不在」。"""







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



# ---------------------------------------------------------------------------
# 三、前端契约：布局（拖拽 + 折叠）
# ---------------------------------------------------------------------------
class TestLayoutContract:
    """面板布局必须由**真的 DOM** + **CSS 变量**驱动。"""















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











# ---------------------------------------------------------------------------
# 五、前端契约：路径校验与空态
# ---------------------------------------------------------------------------
class TestPathValidationContract:
    """接入前校验与空态提示必须在前端落地。"""








# ---------------------------------------------------------------------------
# 六、前端契约：流水区的问答高亮
# ---------------------------------------------------------------------------
class TestFlowHighlightContract:
    """用户提问与 Agent 回答要能被一眼认出来。"""






# ---------------------------------------------------------------------------
# 七、前端逻辑：**真的跑一遍**（Node 沙箱）
# ---------------------------------------------------------------------------
class TestFrontendBehaviour:
    """用 Node 把 dashboard.js 跑起来，断言它的**行为**。"""




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
