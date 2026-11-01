"""实时审核流水（Live Audit Log）—— 把 ON_TRACK 判定压成一行灰字（v3.3.1）。

为什么要有这个
==============

L1 规则引擎对**每一个**事件都会给出一个判定，绝大多数是 ``ON_TRACK``。
但 ``ON_TRACK`` 在旧版本里只被**计数**，从不外送 ——
用户盯着看板，只看到「什么也没发生」，
完全看不出「哨兵其实一直在看」。

v3.3.1 把这类判定压成**一行**极简灰字：

    [12:01:05] 正常读取: auth.py
    [12:01:06] 正常执行: pytest

三条硬约束
==========

1. **不显示完整 JSON** —— 完整事件在右侧「被监控日志」里已经有了，
   这里只留**意图摘要**。
2. **摘要由后端生成** —— 前端只负责渲染。
   这样「极其精简」才有唯一的解释者，也才写得出一行断言来测。
3. **失败绝不抛异常** —— 摘要生成不出来就退回一个通用词，
   绝不让「锦上添花」的流水功能拖垮审查主链路（架构红线 R5）。

跳过的两类事件
==============

* ``tool_result`` —— 只是动作的**回执**，动作本身已经记过一行，
  再记一次会让同一次操作在流水里出现两遍。
* ``usage`` —— 账单事件，引擎在判定之前就已经把它排除掉了。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Mapping, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_DETAIL_LENGTH",
    "MAX_SNIPPET_LENGTH",
    "extract_snippet",
    "first_sentences",
    "format_clock",
    "strip_code_blocks",
    "summarize_event",
]

#: 摘要里「细节」部分的最大字符数。
MAX_DETAIL_LENGTH = 48

#: 「一句人话」里引用的原文最多保留多少个字符（v3.5.1）。
#:
#: 为什么单独一个上限：``MAX_DETAIL_LENGTH`` 是给「正常读取: auth.py」
#: 这种**标签式**摘要用的；v3.5.1 要的是「Agent 正在思考：<原文前 40 字>」
#: —— 引的是 Agent **自己说的话**，太长会把流水区撑爆。
MAX_SNIPPET_LENGTH = 40

#: 事件正文里常见的「框架噪声段」标签（v3.6.0）。
#:
#: 这些标签是各家 Agent CLI 自己塞进用户消息里的框架提示
#: （工作目录、可用工具、系统提醒…），**不是用户说的话**。
#:
#: ⚠️ 这份清单必须跟前端 ``src/gui/static/dashboard.js`` 里的
#:    ``LOG_NOISE_TAGS`` 保持一致 —— 两边挖的是同一批标签，
#:    不然就会出现「日志面板里干净、流水气泡里全是标签」的分裂。
_NOISE_TAGS: Tuple[str, ...] = (
    "system-reminder",
    "background-task-finished",
    "agent-context",
    "environment_context",
    "skills_instructions",
    "cwd",
)

#: 精确匹配的工具名 → 中文动词。
#:
#: 工具名来自各家 Agent CLI（MCode / Claude Code / Codex…），
#: 大小写与写法都不一样（``Bash`` / ``bash`` / ``shell``），
#: 所以先统一成小写再查表。
_EXACT_VERBS: Dict[str, str] = {
    "bash": "执行",
    "shell": "执行",
    "execute": "执行",
    "exec": "执行",
    "run": "执行",
    "terminal": "执行",
    "command": "执行",
    "read": "读取",
    "view": "读取",
    "cat": "读取",
    "open": "读取",
    "write": "写入",
    "edit": "写入",
    "create": "写入",
    "update": "写入",
    "multiedit": "写入",
    "apply_patch": "写入",
    "notebookedit": "写入",
    "grep": "搜索",
    "search": "搜索",
    "glob": "搜索",
    "rg": "搜索",
    "find": "搜索",
    "ls": "浏览",
    "dir": "浏览",
    "list": "浏览",
    "tree": "浏览",
    "webfetch": "访问",
    "web_fetch": "访问",
    "fetch": "访问",
    "websearch": "访问",
    "web_search": "访问",
    "todowrite": "规划",
    "todoread": "规划",
    "todo": "规划",
    "task": "规划",
    "plan": "规划",
}

#: 包含匹配的工具名片段 → 中文动词（**按顺序**，先命中先用）。
#:
#: 片段一律 **≥ 4 个字符** —— 短的（``ls`` / ``cat`` / ``rg``）做包含匹配
#: 很容易误伤别的词（``location`` 里也有 ``cat``），
#: 那几个名字只做精确匹配。
_FRAGMENT_VERBS: Tuple[Tuple[str, str], ...] = (
    ("webfetch", "访问"),
    ("web_fetch", "访问"),
    ("websearch", "访问"),
    ("web_search", "访问"),
    ("todowrite", "规划"),
    ("notebookedit", "写入"),
    ("apply_patch", "写入"),
    ("write", "写入"),
    ("edit", "写入"),
    ("create", "写入"),
    ("read", "读取"),
    ("view", "读取"),
    ("notebookread", "读取"),
    ("search", "搜索"),
    ("grep", "搜索"),
    ("glob", "搜索"),
    ("browse", "浏览"),
    ("list", "浏览"),
    ("execute", "执行"),
    ("shell", "执行"),
    ("bash", "执行"),
    ("terminal", "执行"),
    ("command", "执行"),
)

#: 工具参数里可能放文件路径的键（按优先级）。
_FILE_KEYS: Tuple[str, ...] = (
    "file_path",
    "filePath",
    "filepath",
    "notebook_path",
    "notebookPath",
    "target_file",
    "targetFile",
    "absolute_path",
    "absolutePath",
    "filename",
    "fileName",
    "path",
    "file",
)

#: 工具参数里可能放搜索模式的键（按优先级）。
_PATTERN_KEYS: Tuple[str, ...] = ("pattern", "query", "regex")

#: 工具参数里可能放网址的键（按优先级）。
_URL_KEYS: Tuple[str, ...] = ("url", "uri", "link")

#: 工具参数里可能放目录的键（按优先级）。
_DIR_KEYS: Tuple[str, ...] = ("path", "dir", "directory", "target")

#: 非工具类事件的中文说法。
_PLAIN_SUMMARIES: Dict[str, str] = {
    "thinking": "正常思考",
    "text": "正常输出",
    "user_message": "正常接收指令",
    "result": "正常完成",
    "tool_result": "正常完成",
}


def _normalize_tool(name: str) -> str:
    """把工具名统一成小写下划线写法。

    :param name: 原始工具名。
    :return: 归一化后的名字。
    """
    text = str(name or "").strip().lower()
    return text.replace("-", "_").replace(" ", "_")


def _verb_for(tool_name: str) -> str:
    """把工具名映射成一个中文动词。

    先精确匹配整个名字，再退化到「包含」匹配 ——
    ``str_replace_editor`` 这种非标准名字也能落到「写入」。

    :param tool_name: 工具名。
    :return: 中文动词；认不出来时返回 ``处理``。
    """
    name = _normalize_tool(tool_name)
    if not name:
        return "处理"
    exact = _EXACT_VERBS.get(name)
    if exact:
        return exact
    for fragment, verb in _FRAGMENT_VERBS:
        if fragment in name:
            return verb
    return "处理"


def _truncate(text: str, limit: int = MAX_DETAIL_LENGTH) -> str:
    """把细节压到指定长度以内（超了就截断并加省略号）。

    :param text: 原始文本。
    :param limit: 最大长度。
    :return: 压短后的单行文本。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)] + "…"


def strip_code_blocks(text: Any) -> str:
    """剥离正文里所有被 ``` 包裹的**代码块**（v3.9.0）。

    为什么要有这一步
    ===============

    流水区要的是**新闻快讯**，不是**代码审查报告**。

    Agent 的思考里经常贴一段 200 行的 diff，用户的消息里
    经常贴一段配置样例 —— 那些是**材料**，不是**标题**。
    极限档位（100）下，一个没剥离的代码块能把一行摘要撑成整屏，
    用户看到的就不是「刚才发生了什么」，而是「刚才贴了什么代码」。

    判定规则
    ========

    * 只有**行首**（允许前导空白）出现 ```` ``` ```` 才算围栏 ——
      句中提到的 ```` ``` ````（比如「用三个反引号包起来」）不算；
    * 围栏行本身**整行丢掉**；
    * 围栏之间的内容**整段丢掉**；
    * **未闭合**的围栏（只有开标记）会把**后面全部**丢掉 ——
      日志被截断时围栏常常只剩开标记，留着就等于把代码漏出去。

    ⚠️ 与前端的 ``stripLogCodeBlocks``（``static/dashboard.js``）
       **必须同算法** —— 两边挖的是同一批代码块，
       不然就会出现「后端摘要干净、前端日志面板里全是代码」的分裂。
       一致性由 ``tests/test_v390_release.py`` 断言。

    :param text: 原始正文。
    :return: 去掉代码块之后的正文。
    """
    value = str(text or "")
    if "```" not in value:
        return value
    kept: List[str] = []
    in_block = False
    for line in value.split("\n"):
        if line.strip().startswith("```"):
            # 围栏行本身丢掉，并切换「在不在代码块里」。
            in_block = not in_block
            continue
        if not in_block:
            kept.append(line)
    return "\n".join(kept).strip()


def first_sentences(text: Any, count: int = 2) -> str:
    """只取正文的前 ``count`` 句话（v3.9.0）。

    为什么
    ====

    极限档位（100）要的是**快讯** —— 一句话说清「刚才发生了什么」。
    一段话里塞三个从句的长句，用户扫一眼抓不到重点。

    怎么算「一句」
    ===========

    * 中文句末标点：``。！？；``；
    * 英文句末标点：``.`` / ``!`` / ``?``，
      但**必须后面是空白或文本结尾**才算 ——
      否则 ``engine.py`` / ``v1.5`` 会被从中间劈开。

    ⚠️ 与前端的 ``firstSentences``（``static/dashboard.js``）
       **必须同算法**（一致性由 ``tests/test_v390_release.py`` 断言）。

    :param text: 原始正文。
    :param count: 最多取几句。
    :return: 前几句组成的文本；不足 ``count`` 句时返回全文。
    """
    value = str(text or "").strip()
    if not value or count <= 0:
        return ""
    length = len(value)
    index = 0
    end = 0
    found = 0
    while index < length and found < count:
        char = value[index]
        if char in "。！？!?；;":
            index += 1
            end = index
            found += 1
            continue
        if char in ".!":
            nxt = value[index + 1] if index + 1 < length else ""
            if nxt == "" or nxt.isspace():
                index += 1
                end = index
                found += 1
                continue
        index += 1
    if found < count:
        return value
    return value[:end].strip()


def _strip_noise_sections(text: Any) -> str:
    """把正文里的「框架噪声段」**挖掉**（v3.6.0）。

    ⚠️ 为什么后端也要挖：
       噪声标签是各家 Agent CLI 自己塞进**用户消息**里的框架提示
       （工作目录、可用工具、系统提醒…），**不是用户说的话**。
       前端渲染日志面板时会把它们按段落挖掉，可后端摘「一句人话」
       的原文时是**先截断、后挖**的 —— 40 字一刀下去，
       ``</environment_context>`` 这个**闭合标签先被切没了**，
       前端再挖就只剩个光秃秃的 ``<cwd>`` 开标签。

       结果就是：流水区那个「🧑 用户提问」气泡里，
       显示的既不是用户的提问，也不是完整的框架提示，
       而是半截 ``<cwd>D:\\项目\\…`` —— 两头不靠。

       所以顺序必须是**先挖、再截**。

    ⚠️ 为什么是「挖段」而不是「整条丢掉」：
       codex 的用户消息里，真实问题跟 ``<environment_context>``
       挤在**同一条**消息里 —— 整条丢掉，用户的问题也就没了。

    :param text: 原始正文。
    :return: 去掉噪声段落之后的正文。
    """
    value = str(text or "")
    for tag in _NOISE_TAGS:
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        while True:
            start = value.find(open_tag)
            if start == -1:
                break
            end = value.find(close_tag, start + len(open_tag))
            if end == -1:
                # 没有闭合标签 —— 只去掉开标签本身，正文留着。
                value = value[:start] + value[start + len(open_tag):]
            else:
                value = value[:start] + value[end + len(close_tag):]
    # 独立成行的噪声标记（没有成对的标签）。
    value = re.sub(
        r"^[ \t]*session_identity_reminder.*$",
        "",
        value,
        flags=re.MULTILINE,
    )
    return value.strip()


def _command_detail(command: str) -> str:
    """从命令行里取出「最有代表性的一截」。

    取第一个词；第二个词不是选项（``-`` / ``--`` 开头）时也带上 ——
    ``git status`` 比光秃秃一个 ``git`` 有用得多。

    :param command: 命令文本。
    :return: 细节文本；命令为空时返回空串。
    """
    text = str(command or "").strip()
    if not text:
        return ""
    parts = text.split()
    first = parts[0].strip("\"'")
    if len(parts) > 1 and not parts[1].startswith("-"):
        return _truncate(first + " " + parts[1])
    if "/" in first or "\\" in first:
        base = re.split(r"[\\/]", first)[-1]
        if base:
            return _truncate(base)
    return _truncate(first)


def _file_detail(path: str) -> str:
    """把文件路径压成「末一到两段」。

    ``D:\\work\\project\\src\\core\\engine.py`` → ``core/engine.py``；
    ``auth.py`` → ``auth.py``。

    :param path: 文件路径。
    :return: 细节文本；路径为空时返回空串。
    """
    text = str(path or "").strip().strip("\"'")
    if not text:
        return ""
    parts = [item for item in re.split(r"[\\/]", text) if item]
    if not parts:
        return ""
    if len(parts) == 1:
        return _truncate(parts[0])
    return _truncate("/".join(parts[-2:]))


def _url_detail(url: str) -> str:
    """把网址压成「域名」（没有域名就退回路径）。

    :param url: 网址文本。
    :return: 细节文本；网址为空时返回空串。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return _truncate(text)
    if parts.netloc:
        return _truncate(parts.netloc)
    return _truncate(parts.path or text)


def _first_argument(arguments: Mapping[str, Any], keys: Tuple[str, ...]) -> str:
    """按优先级取出第一个非空的字符串参数。

    :param arguments: 工具参数字典。
    :param keys: 候选键名（按优先级）。
    :return: 参数值；没有可用值时返回空串。
    """
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _detail_for(tool: Mapping[str, Any], verb: str) -> str:
    """按动词类别挑出「值得显示的那一截细节」。

    :param tool: 工具信息字典（含 ``name`` / ``command`` / ``arguments``）。
    :param verb: 已经判定好的中文动词。
    :return: 细节文本；挑不出来时返回空串。
    """
    arguments = tool.get("arguments")
    arguments = arguments if isinstance(arguments, Mapping) else {}
    # v3.9.0：命令里可能带代码块（heredoc 之类）—— 先剥离再挑细节。
    command = strip_code_blocks(str(tool.get("command") or ""))

    if verb == "执行":
        return _command_detail(command)
    if verb in ("读取", "写入"):
        detail = _first_argument(arguments, _FILE_KEYS)
        if detail:
            return _file_detail(detail)
    if verb == "搜索":
        detail = _first_argument(arguments, _PATTERN_KEYS)
        if detail:
            return _truncate(detail)
        return _command_detail(command)
    if verb == "访问":
        detail = _first_argument(arguments, _URL_KEYS)
        if detail:
            return _url_detail(detail)
    if verb == "浏览":
        detail = _first_argument(arguments, _DIR_KEYS)
        if detail:
            return _file_detail(detail)

    # 兜底：任何认不出来的工具，只要带命令就把命令显示出来。
    if command:
        return _command_detail(command)
    return ""


def summarize_event(event: Any) -> str:
    """把一个事件压成一行极简摘要。

    :param event: 统一事件字典（来自适配器）。
    :return: 形如 ``正常读取: auth.py`` 的摘要；
        实在压不出来时返回 ``正常处理``。
    """
    if not isinstance(event, Mapping):
        return "正常处理"
    event_type = str(event.get("type") or "")
    if event_type != "tool_call":
        return _PLAIN_SUMMARIES.get(event_type, "正常处理")
    tool = event.get("tool")
    tool = tool if isinstance(tool, Mapping) else {}
    name = str(tool.get("name") or "")
    verb = _verb_for(name)
    detail = _detail_for(tool, verb)
    if not detail:
        # 认不出来的工具 → 把工具名本身显示出来，
        # 免得流水里只有一句没头没脑的「正常处理」。
        if verb == "处理" and name:
            return f"正常处理: {_truncate(name)}"
        return f"正常{verb}"
    return f"正常{verb}: {detail}"


def extract_snippet(event: Any, limit: int = MAX_SNIPPET_LENGTH) -> str:
    """从事件里抽出「一句人话」要引用的原文（v3.5.1）。

    v3.5.1 的流水区要说的是**人话**：
    ``[15:32] Agent 正在思考：关键发现是…``。
    光有「正常读取: auth.py」这种**标签式**摘要不够 ——
    用户真正想知道的是「Agent 到底在说什么、在做什么」。

    所以这里从事件里取两样东西：

    * ``user_message`` / ``thinking`` / ``text`` 事件 → 正文的前若干字；
    * ``tool_call`` 事件 → 命令行（取不到就退到文件路径 / 工具名）。

    ⚠️ v3.6.0 补上 ``user_message`` ——
       流水区现在把「用户提问」当成**核心上下文**高亮出来，
       可正文取不到的话，那个气泡里只有「🧑 用户提问：…」，
       后面是空的 —— 用户还是不知道自己问了什么。

    ⚠️ 刻意**只取前若干字** —— 原文可能有整屏那么长，
    全塞进流水区会把面板撑爆，而且用户根本读不完。

    ⚠️ v3.6.0：**先挖噪声段、再截断** ——
       顺序反了的话，40 字一刀会把 ``</environment_context>``
       这种闭合标签切没，前端再挖就挖不干净，
       「🧑 用户提问」气泡里剩下的就是半截 ``<cwd>…``，
       既不是用户问的，也不是完整提示。

    :param event: 统一事件字典（来自适配器）。
    :param limit: 最多保留多少个字符。
    :return: 细节文本；取不到时返回空串。
    """
    if not isinstance(event, Mapping):
        return ""
    event_type = str(event.get("type") or "")
    if event_type in ("user_message", "thinking", "text"):
        # ⚠️ v3.9.0：**先挖、再截** —— 代码块与噪声段都必须在截断**之前**
        #    挖掉；顺序反了的话，40 字一刀会把围栏切在中间，
        #    前端再挖就挖不干净，流水里剩下的就是半截代码。
        cleaned = strip_code_blocks(event.get("text"))
        return _truncate(_strip_noise_sections(cleaned), limit)
    if event_type != "tool_call":
        return ""
    tool = event.get("tool")
    tool = tool if isinstance(tool, Mapping) else {}
    command = str(tool.get("command") or "").strip()
    if command:
        # v3.9.0：命令行里也可能带代码块（heredoc 之类）。
        return _truncate(strip_code_blocks(command), limit)
    # 命令行没有 → 退到「这个工具动了哪个文件 / 搜了什么」。
    verb = _verb_for(str(tool.get("name") or ""))
    detail = _detail_for(tool, verb)
    if detail:
        return _truncate(detail, limit)
    name = str(tool.get("name") or "").strip()
    if name:
        return _truncate(name, limit)
    return ""


def format_clock(timestamp: Any) -> str:
    """把 Unix 时间戳格式化成 ``HH:MM:SS``（本机本地时区）。

    :param timestamp: Unix 时间戳（秒）；非法时退回当前时间。
    :return: 形如 ``12:01:05`` 的时间文本。
    """
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        value = time.time()
    try:
        return time.strftime("%H:%M:%S", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return time.strftime("%H:%M:%S")
