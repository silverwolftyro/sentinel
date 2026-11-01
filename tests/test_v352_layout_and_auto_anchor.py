"""v3.5.2 回归：中间面板**不再重叠** · 任务锚点**自动拔取**。

背景
====

v3.5.1 的日志跟随与自然语言流水都很好，但 GUI 上出了**灾难级**问题：

1. 中间面板的【实时审核流水】直接**盖在**【实时告警】的卡片上，
   字全部叠在一起，完全没法看。
   根因是 v3.3.1 的设计：流水被做成 ``position: absolute`` 的
   **背景层**，告警卡片靠 ``z-index: 2`` **压在上面**。
2. 上一轮遗留的「雷达挂载会话无法自动提取初始 Prompt」一直没修。

这一版把两件事都钉住：

* 中间面板**彻底改成 Flex 纵向布局** —— 上下分离，各占各的行；
* 任务锚点**自动拔取** —— 日志头部 → 命令行 → 手动输入，
  三级回退，系统噪声必须被过滤掉。

⚠️ 「自动提取」有一条**铁律**：宁可找不到，也不能找错 ——
   把 ``<system-reminder>`` 当成任务目标，会凭空造出假锚点，
   后面的意图漂移判定全会跟着跑偏。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.procscan import prompt_from_cmdline_args
from src.radar import (
    TASK_GOAL_MAX_CHARS,
    TASK_GOAL_SCAN_LINES,
    DiscoveredSession,
    describe_session,
    extract_task_goal,
    extract_task_goal_from_cmdline,
    resolve_task_goal,
)

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _css_rules(css: str, selector: str) -> list:
    """取出 CSS 里某个选择器的**全部**规则体（去注释）。

    :param css: 整份 CSS 文本。
    :param selector: 选择器（原文）。
    :return: 规则体列表（按出现顺序）。
    """
    pattern = re.compile(
        r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}"
    )
    bodies = []
    for match in pattern.finditer(css):
        body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
        bodies.append(body)
    return bodies


def _css_rule(css: str, selector: str) -> str:
    """取出 CSS 里某个选择器的**最后一条**规则体（层叠结果）。

    :param css: 整份 CSS 文本。
    :param selector: 选择器（原文）。
    :return: 规则体文本；没有就是空串。
    """
    bodies = _css_rules(css, selector)
    return bodies[-1] if bodies else ""


def _css_media(css: str, condition: str) -> str:
    """抽出某个媒体查询块的**全部内容**（按大括号配平）。

    :param css: 整份 CSS 文本。
    :param condition: 媒体查询条件（如 ``max-width: 760px``）。
    :return: 媒体块内部文本；没找到就是空串。
    """
    index = css.find("@media (" + condition + ")")
    if index < 0:
        return ""
    start = css.find("{", index)
    if start < 0:
        return ""
    depth = 0
    for pos in range(start, len(css)):
        if css[pos] == "{":
            depth += 1
        elif css[pos] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:pos]
    return ""


def _css_block(scope: str, selector: str) -> str:
    """在给定作用域内取选择器的规则体（**允许缩进**）。

    媒体查询里的规则是缩进写的，所以不能只认顶格的写法。

    :param scope: CSS 文本片段。
    :param selector: 选择器（不含前导空白）。
    :return: 最后一条规则体；没有就是空串。
    """
    pattern = re.compile(
        r"(?m)^[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}"
    )
    bodies = []
    for match in pattern.finditer(scope):
        body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
        bodies.append(body)
    return bodies[-1] if bodies else ""


def _write_jsonl(path: Path, lines) -> str:
    """写一份 JSONL 日志并返回路径。

    :param path: 目标路径。
    :param lines: 载荷列表。
    :return: 路径字符串。
    """
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    path.write_text(body + "\n", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# 一、布局契约：流水与告警必须**上下分离**
# ---------------------------------------------------------------------------
class TestMiddlePanelLayout:
    """中间面板（实时告警 + 实时流水）的结构契约。"""

    @staticmethod
    def _css() -> str:
        """读看板样式表。

        :return: CSS 全文。
        """
        return _read("src", "gui", "static", "dashboard.css")

    def test_告警面板是Flex纵向容器(self):
        """``display: flex; flex-direction: column;`` 必须写死。"""
        block = _css_rule(self._css(), ".panel--alerts")
        assert "display: flex;" in block
        assert "flex-direction: column;" in block
        assert "min-height: 0;" in block

    def test_上半告警区吃掉剩余高度并自己滚(self):
        """任务书原文：``flex: 1; overflow-y: auto; min-height: 0;``。"""
        block = _css_rule(self._css(), ".panel--alerts .panel__body")
        assert re.search(r"flex:\s*1\b", block)
        assert "min-height: 0;" in block
        assert "overflow-y: auto;" in block

    def test_流水区不再是绝对定位(self):
        """**绝不能**再有 ``position: absolute`` —— 那是重叠的根源。"""
        block = _css_rule(self._css(), ".live-audit")
        assert "position: absolute" not in block
        assert "position: fixed" not in block
        assert "position: static;" in block

    def test_流水区不被压缩且有固定高度(self):
        """任务书原文：``flex-shrink: 0`` + 固定高度（200px 或 30vh）。

        ⚠️ v3.6.0 改了**机制**，没改**意图** ——
        高度改由 CSS 变量 ``--audit-h`` 驱动（用户可以用拖拽条改），
        默认值仍然钉在 200px；``max-height: 30vh`` 被**刻意去掉**，
        因为写死的上限会在大屏上把用户拖出来的比例截断。
        """
        css = self._css()
        block = _css_rule(css, ".live-audit")
        assert re.search(r"flex:\s*0\s+0\s+auto", block)
        assert re.search(r"height:\s*var\(--audit-h\)", block)
        # 默认值仍钉在中间面板上（变量的宿主）。
        alerts = _css_rule(css, ".panel--alerts")
        assert re.search(r"--audit-h:\s*200px", alerts)
        # 刻意不再写死 30vh 上限（v3.6.0 起的拖拽需要它）。
        assert "max-height: 30vh;" not in block

    def test_流水行自己滚(self):
        """流水区内部自带滚动 —— 否则新行会把面板撑破。"""
        block = _css_rule(self._css(), ".live-audit__lines")
        assert "flex: 1;" in block
        assert "min-height: 0;" in block
        assert "overflow-y: auto;" in block

    def test_旧的让位内边距已经去掉(self):
        """``padding-bottom: 182px`` 是为绝对定位的流水层预留的 —— 没了。"""
        css = self._css()
        assert "padding-bottom: 182px" not in css
        block = _css_rule(css, ".panel--alerts .panel__body")
        assert "padding-bottom: 12px;" in block

    def test_标题条不再靠z索引压住流水(self):
        """流水标题不再需要「爬到别人上面」—— 各安其位。"""
        css = self._css()
        head = _css_rule(css, ".live-audit__head")
        assert "z-index" not in head
        assert "position: absolute" not in head

    def test_告警卡片不再靠z索引压住流水(self):
        """卡片也不再有 ``z-index: 2`` —— 中间面板里不该有层级打架。"""
        css = self._css()
        card = _css_rule(css, ".alert-card")
        assert "z-index" not in card
        # 中间面板这块不许再出现任何 z-index。
        alerts_block = css.split(".panel--alerts {", 1)[1].split("\n}", 1)[0]
        assert "z-index" not in alerts_block

    def test_流水区不再拦截指针事件(self):
        """它现在是**正常的一行**，必须能被点、能被滚。"""
        block = _css_rule(self._css(), ".live-audit")
        assert "pointer-events: none" not in block

    def test_窄屏下流水区要收窄(self):
        """760px 断点下面板最小高度只有 260px，流水区得让位。

        ⚠️ v3.6.0 起改的是**变量**而不是 ``.live-audit`` 本体 ——
        窄屏把 ``--audit-h`` 压到 140px，高度自然就窄了。
        """
        mobile = _css_media(self._css(), "max-width: 760px")
        assert ".panel--alerts {" in mobile
        block = _css_block(mobile, ".panel--alerts")
        assert re.search(r"--audit-h:\s*140px", block)

    def test_HTML结构里流水在分隔条上方(self):
        """DOM 顺序必须和 Flex 顺序一致。

        ⚠️ v3.6.1 **改过顺序** ——
           v3.5.2~v3.6.0 是「标题 → 告警列表 → 分隔条 → 流水」，
           流水区被面板底边钉死，分隔条挂在它**动不了**的上沿上，
           于是往下拖时把手反向跑（详见 tests/test_v361_drag_direction_fix.py）。
           现在流水区挪到分隔条**上方**：
           流水 → 分隔条 → 标题 → 告警列表。
        """
        html = _read("src", "gui", "templates", "dashboard.html")
        block = html.split('class="panel panel--alerts"', 1)[1]
        live = block.index('class="live-audit"')
        resizer = block.index('id="resizer-audit"')
        head = block.index('class="panel__head"')
        body = block.index('class="panel__body"')
        assert live < resizer < head < body

    def test_旧的背景层注释已经改掉(self):
        """注释不能还在教人「卡片压流水」—— 那是旧设计。"""
        html = _read("src", "gui", "templates", "dashboard.html")
        assert "盖在它上面" not in html
        css = self._css()
        assert "告警卡片会盖在流水上面" not in css


# ---------------------------------------------------------------------------
# 二、锚点提取：各种真实日志形态
# ---------------------------------------------------------------------------
class TestExtractTaskGoal:
    """``extract_task_goal`` —— 从日志头部把任务目标挖出来。"""

    def test_mcode续接会话的Goal摘要(self, tmp_path):
        """mcode 续接会话首行是 ``compactionSummary``，任务在 ``## Goal`` 段。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {
                "message_id": "m-1",
                "message": {
                    "role": "compactionSummary",
                    "summary": (
                        "## Goal\n\n"
                        "完成 v3.5.2：修流水重叠 + 自动拔锚点\n\n"
                        "## Constraints\n\n- 诚实优先"
                    ),
                },
            },
        ])
        goal = extract_task_goal(path)
        assert goal.startswith("完成 v3.5.2")
        assert "Constraints" not in goal

    def test_mcode新会话的用户消息(self, tmp_path):
        """``{"message":{"role":"user","content":[...]}}``。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {
                "message_id": "m-1",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "帮我新建一个 hello.py"},
                    ],
                },
            },
        ])
        assert extract_task_goal(path) == "帮我新建一个 hello.py"

    def test_codex的response_item(self, tmp_path):
        """codex 藏在 ``payload`` 里 —— 写死一条路径就会漏。"""
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            {
                "timestamp": "2026-10-12T02:16:34.877Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "帮我写个单元测试"},
                    ],
                },
            },
        ])
        assert extract_task_goal(path) == "帮我写个单元测试"

    def test_claude的用户消息(self, tmp_path):
        """claude 是 ``{"type":"user","message":{"content":"..."}}``。"""
        path = _write_jsonl(tmp_path / "session.jsonl", [
            {
                "type": "user",
                "message": {"role": "user", "content": "把登录模块重构一下"},
            },
        ])
        assert extract_task_goal(path) == "把登录模块重构一下"

    def test_v06的item事件(self, tmp_path):
        """mcode v0.6：``item.type == "user_message"``。"""
        path = _write_jsonl(tmp_path / "events.jsonl", [
            {
                "type": "item.completed",
                "item": {
                    "type": "user_message",
                    "content": [{"type": "text", "text": "修复登录超时"}],
                },
            },
        ])
        assert extract_task_goal(path) == "修复登录超时"

    def test_扁平形态(self, tmp_path):
        """``{"type":"user_message","content":"..."}``。"""
        path = _write_jsonl(tmp_path / "flat.jsonl", [
            {"type": "user_message", "content": "把 README 翻译成中文"},
        ])
        assert extract_task_goal(path) == "把 README 翻译成中文"

    def test_只扫前十行(self, tmp_path):
        """任务书原文：「自动读取文件前 10 行」。"""
        lines = [
            {"type": "event_msg", "payload": {"type": "noise"}},
            {"type": "event_msg", "payload": {"type": "noise"}},
        ]
        # 第 11 行才出现用户消息 —— 前 10 行扫不到。
        lines.extend(
            {"type": "event_msg", "payload": {"type": "noise"}}
            for _ in range(8)
        )
        lines.append(
            {"type": "user_message", "content": "太靠后了，扫不到"}
        )
        path = _write_jsonl(tmp_path / "late.jsonl", lines)
        assert extract_task_goal(path) == ""
        assert TASK_GOAL_SCAN_LINES == 10

    def test_坏行不影响后续行(self, tmp_path):
        """解析不了的行**跳过** —— 不能让一行脏数据毁掉整份提取。"""
        path = tmp_path / "broken.jsonl"
        path.write_text(
            "{不是合法 JSON\n"
            + json.dumps(
                {"type": "user_message", "content": "还是能提取到"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        assert extract_task_goal(str(path)) == "还是能提取到"

    def test_截断到二百字(self, tmp_path):
        """锚点只是锚点 —— 太长会挤爆输入框。"""
        path = _write_jsonl(tmp_path / "long.jsonl", [
            {"type": "user_message", "content": "长" * 500},
        ])
        goal = extract_task_goal(path)
        assert len(goal) == TASK_GOAL_MAX_CHARS
        assert TASK_GOAL_MAX_CHARS == 200


# ---------------------------------------------------------------------------
# 三、系统噪声必须被过滤（**宁可找不到，也不能找错**）
# ---------------------------------------------------------------------------
class TestNoiseRejected:
    """系统注入的内容**绝不能**被当成任务目标。"""

    def test_codex的环境上下文不算目标(self, tmp_path):
        """codex 把 ``<environment_context>`` 塞进第一条 user 消息。"""
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "<environment_context>\n"
                                "  <cwd>D:\\work</cwd>\n"
                                "</environment_context>"
                            ),
                        },
                    ],
                },
            },
        ])
        assert extract_task_goal(path) == ""

    def test_噪声在前真话在后要取到真话(self, tmp_path):
        """同一份日志里，噪声被跳过、后面那句真话被取到。"""
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "<system-reminder>\n"
                                "<agent-context>…</agent-context>\n"
                                "</system-reminder>"
                            ),
                        },
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "帮我把登录修好"},
                    ],
                },
            },
        ])
        assert extract_task_goal(path) == "帮我把登录修好"

    def test_会话身份提醒不算目标(self, tmp_path):
        """mcode 的 ``session_identity_reminder`` —— 那是机器自己的话。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {
                "message": {
                    "role": "custom",
                    "customType": "session_identity_reminder",
                    "content": (
                        "<system-reminder>\n"
                        "YOUR SESSION ID: mvs_abcdef\n"
                        "</system-reminder>"
                    ),
                },
            },
        ])
        assert extract_task_goal(path) == ""

    def test_助手消息不算目标(self, tmp_path):
        """Agent 自己说的话不是用户的目标。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "我想想该怎么做"},
                    ],
                },
            },
        ])
        assert extract_task_goal(path) == ""

    def test_开发者指令不算目标(self, tmp_path):
        """codex 的 ``developer`` 角色是系统提示。"""
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<skills_instructions>\n…\n</skills_instructions>",
                        },
                    ],
                },
            },
        ])
        assert extract_task_goal(path) == ""


# ---------------------------------------------------------------------------
# 四、命令行回退
# ---------------------------------------------------------------------------
class TestCmdlineFallback:
    """日志里没有时，去命令行里找。"""

    def test_认出命令行里的用户输入(self):
        """``mcode exec "把登录模块重构一下"``。"""
        args = [
            r"C:\Users\你\node.exe",
            r"C:\Users\你\node_modules\@minimax-ai\code\cli.js",
            "exec",
            "把登录模块重构一下",
        ]
        assert prompt_from_cmdline_args(args) == "把登录模块重构一下"

    def test_开关不是用户输入(self):
        """``--continue`` 这种开关不能被当成任务目标。"""
        args = ["node.exe", "cli.js", "--continue", "--verbose"]
        assert prompt_from_cmdline_args(args) == ""

    def test_子命令不是用户输入(self):
        """``exec`` / ``run`` 这类子命令也不是。"""
        args = ["node.exe", "cli.js", "exec", "run"]
        assert prompt_from_cmdline_args(args) == ""

    def test_脚本路径不是用户输入(self):
        """带路径分隔符的一律是路径。"""
        args = ["node.exe", "cli.js", "D:\\work\\some-file.txt"]
        assert prompt_from_cmdline_args(args) == ""

    def test_空命令行不报错(self):
        """什么都识别不出就是空串 —— 绝不抛异常。"""
        assert prompt_from_cmdline_args([]) == ""
        assert prompt_from_cmdline_args(["node.exe"]) == ""

    def test_radar的转发函数可用(self):
        """``extract_task_goal_from_cmdline`` 是公开入口。"""
        args = ["node.exe", "cli.js", "exec", "帮我修一下登录"]
        assert extract_task_goal_from_cmdline(args) == "帮我修一下登录"


# ---------------------------------------------------------------------------
# 五、三级回退 + 会话描述
# ---------------------------------------------------------------------------
class TestResolveTaskGoal:
    """日志 → 命令行 → 空串。"""

    def test_优先用日志(self, tmp_path):
        """日志里有就用日志里的。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {"type": "user_message", "content": "日志里的目标"},
        ])
        index = {"d:\\work": "命令行里的目标"}
        goal = resolve_task_goal(path, cwd="D:\\work", prompt_index=index)
        assert goal == "日志里的目标"

    def test_日志没有就退到命令行(self, tmp_path):
        """日志里没有 → 按工作目录去命令行索引里找。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ])
        index = {"d:\\work": "命令行里的目标"}
        goal = resolve_task_goal(path, cwd="D:\\work", prompt_index=index)
        assert goal == "命令行里的目标"

    def test_都没有就是空串(self, tmp_path):
        """一条都找不到 → 空串（界面据此提示手动输入）。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ])
        assert resolve_task_goal(path, cwd="D:\\work") == ""

    def test_工作目录对不上就不乱认(self, tmp_path):
        """工作目录不一样 → 不认（宁可没有也不给错的）。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ])
        index = {"d:\\other": "别的目录的目标"}
        assert resolve_task_goal(path, cwd="D:\\work", prompt_index=index) == ""


class TestSessionCarriesGoal:
    """会话描述里要带上自动拔出来的目标。"""

    def test_会话字段里有task_goal(self):
        """``DiscoveredSession`` 要有 ``task_goal`` 字段。"""
        session = DiscoveredSession(
            path="C:\\a\\b.jsonl",
            agent="mcode",
            agent_label="MCode",
            cwd="D:\\work",
            label="[MCode] D:\\work",
            size=10,
            mtime=1.0,
            age=0.0,
            active=True,
            task_goal="我要修登录",
        )
        assert session.task_goal == "我要修登录"
        assert session.to_dict()["task_goal"] == "我要修登录"

    def test_默认没有目标(self):
        """默认是空串 —— 不是「未设置」字符串。"""
        session = DiscoveredSession(
            path="C:\\a\\b.jsonl",
            agent="mcode",
            agent_label="MCode",
            cwd="",
            label="[MCode]",
            size=10,
            mtime=1.0,
            age=0.0,
            active=True,
        )
        assert session.task_goal == ""
        assert session.to_dict()["task_goal"] == ""

    def test_describe_session会自动拔目标(self, tmp_path):
        """描述会话时顺手把目标拔出来。"""
        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {"type": "user_message", "content": "帮我修一下登录超时"},
        ])
        session = describe_session(path)
        assert session is not None
        assert session.task_goal == "帮我修一下登录超时"
        assert session.to_dict()["task_goal"] == "帮我修一下登录超时"

    def test_雷达扫描结果带着目标(self, tmp_path):
        """雷达扫出来的每一条都要带 ``task_goal`` —— 界面直接显示。"""
        from src.radar import scan_agent_sessions

        root = tmp_path / "sessions"
        root.mkdir()
        _write_jsonl(root / "messages.jsonl", [
            {"type": "user_message", "content": "扫描也要带目标"},
        ])

        found = scan_agent_sessions(
            roots=[{
                "agent": "mcode",
                "label": "MCode",
                "path": str(root),
            }],
            max_age=999999.0,
            limit=10,
        )
        assert found
        assert found[0].task_goal == "扫描也要带目标"


# ---------------------------------------------------------------------------
# 六、服务端挂载流程要真的用上这条链
# ---------------------------------------------------------------------------
class TestServerAttachUsesChain:
    """挂载会话必须走「日志 → 命令行 → 适配器」这条链。"""

    def test_服务端有三级回退的入口(self):
        """``_resolve_attached_goal`` 必须存在。"""
        from src.gui import server

        assert hasattr(server, "_resolve_attached_goal")

    def test_挂载流程调用的是新链路(self):
        """``_attach_one`` 里不能再直接调旧的那一级。"""
        from src.gui import server

        source = Path(server.__file__).read_text(encoding="utf-8")
        assert "_resolve_attached_goal(path, agent, cwd)" in source

    def test_日志里的目标能被服务端取到(self, tmp_path):
        """端到端：日志 → ``_resolve_attached_goal`` → 任务目标。"""
        from src.gui.server import _resolve_attached_goal

        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {
                "message": {
                    "role": "compactionSummary",
                    "summary": "## Goal\n\n修复实时流水 UI 重叠\n",
                },
            },
        ])
        goal = _resolve_attached_goal(path, "mcode", "")
        assert goal == "修复实时流水 UI 重叠"

    def test_系统噪声不会被服务端当成目标(self, tmp_path):
        """噪声 → 服务端也必须返回空串。"""
        from src.gui.server import _resolve_attached_goal

        path = _write_jsonl(tmp_path / "messages.jsonl", [
            {
                "message": {
                    "role": "custom",
                    "customType": "session_identity_reminder",
                    "content": "<system-reminder>\nYOUR SESSION ID: x\n</system-reminder>",
                },
            },
        ])
        assert _resolve_attached_goal(path, "mcode", "") == ""


# ---------------------------------------------------------------------------
# 七、雷达界面要把自动提取的目标露出来
# ---------------------------------------------------------------------------
class TestRadarShowsGoal:
    """挂载之前就要告诉用户「不用你手敲」。"""

    def test_前端渲染了自动提取的目标(self):
        """雷达条目里要有那一行。"""
        js = _read("src", "gui", "static", "dashboard.js")
        assert "attach__goal" in js
        assert "item.task_goal" in js
        assert "[自动提取]" in js

    def test_样式里有那一行(self):
        """样式也得有 —— 不然就是一行没样式的裸文字。"""
        css = _read("src", "gui", "static", "dashboard.css")
        assert ".attach__goal {" in css
        assert ".attach__goal-tag {" in css


# ---------------------------------------------------------------------------
# 八、版本口径
# ---------------------------------------------------------------------------
def test_版本号跟当前包版本一致():
    """版本号必须跟当前包版本一致 —— 这里钉住其中一处。

    ⚠️ **动态**断言 —— 写死版本号的话每发一次版就得改一遍，
    而「忘了改」正是版本号不同步的**头号原因**
    （v3.0.1 起 ``test_v300_release.py`` 就已经全面动态化，
    v3.3.2 与 v3.4.0 的测试文件也都已经跟上）。

    「四处一致」由 ``test_v300_release.py`` 的
    :class:`Test版本一致` 与 ``check_version_consistency.py`` 把关。
    """
    import src

    source = _read("src", "__init__.py")
    assert '"{}"'.format(src.__version__) in source
    assert re.match(r"^\d+\.\d+\.\d+$", src.__version__)
