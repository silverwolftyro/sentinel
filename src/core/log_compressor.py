"""日志高信噪比压缩与特征提取引擎。

问题是什么
==========
一次 Agent 会话动辄产出几十 MB 的 JSONL 日志：
一次 ``Read`` 回吐 500 行源码、一次 ``Bash`` 回吐几 KB 编译日志、
一段 ``thinking`` 絮絮叨叨自问自答……
把这些原文塞进模型的 64K 窗口，等于**用废话把窗口堵死**，
真正值钱的「演进事件流」反而埋在垃圾堆底下。

本模块做什么
============
把原始日志压成一条**每行一个语义单元**的演进事件流：

.. code-block:: text

    [Task Goal]: 给旧版鉴权模块补上令牌轮换
    [1] [思考] 先看看现有鉴权实现
    [2] [连续读取] core/auth.py, core/user.py 等 5 个文件
    [3] [Read] core/token.py (成功, 43行)
    [4] [Bash] pytest tests/test_token.py (成功)
    [Current Action]: 正在修复 core/token.py 里的过期判断

四条铁律
========

1. **结果只留元数据** —— ``tool_result`` 的正文（代码、编译日志、
   大段输出）一律丢弃，只留「成功没有 / 多少行 / 出错在哪」。
2. **同类动作合并** —— 连续 5 次 Read 合成一条，不占 5 行。
3. **意图只留一句** —— ``thinking`` 超过 100 字时，
   只保留首句和含「修改 / 重构 / 修复 / 测试」等动词的关键句。
4. **首尾必锚定** —— 第一行永远是 ``[Task Goal]:``，
   最后一行永远是 ``[Current Action]:``，
   让读者一眼看到「要干什么」和「现在干到哪」。

设计约束
========

* **纯函数** —— :func:`compress_events` 只吃事件、只吐文本，
  不读文件、不碰网络、不调模型。
* **绝不抛异常** —— 审查侧失败只能降级，不能中断（红线 R5）。
* **不依赖大模型** —— 全是确定性规则，同样的输入必得同样的输出。
* **不动引擎** —— 本模块是独立处理器，接不接入由调用方决定。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..adapters.base import Event
from ..adapters.claude import ClaudeAdapter
from ..adapters.mcode import McodeAdapter

logger = logging.getLogger(__name__)

__all__ = [
    "CompressedLog",
    "CompressionOptions",
    "compress_events",
    "compress_file",
    "compress_jsonl",
    "extract_intent",
    "fallback_intent",
    "render_stream",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 任务目标的最大字符数（与引擎的 ``GOAL_MAX_CHARS`` 对齐）。
GOAL_MAX_CHARS = 200

#: ``thinking`` 正文的「长文阈值」—— 超过这个长度才走「意图提取」。
THINKING_LONG_THRESHOLD = 100

#: 意图关键词 —— 命中即视为「关键句」。
#:
#: ⚠️ **v4.0.1 扩容**：原表只有「开发类」动词（修改 / 修复 / 测试 ……），
#:    缺了「读取 / 查看 / 生成 / 计算」这类**动作动词**。
#:    后果实测（v4.0.1 A/B 评测，41 窗口）：
#:    「Now let me generate the synthetic "approaching" material.」
#:    这类**转向枢轴句**因句首带「let me」被 :func:`_is_filler` 判成废话，
#:    整句丢掉 —— L2 判定因此看不到「Agent 转去做什么」的信号，
#:    B 臂漏报率从 24.0% 恶化到 40.0%。
#:
#: ⚠️ **不要加单字动词**（「看」「读」「查」）：
#:    「让我先看看这个文件」会因为命中「看」而被当成关键句保留下来，
#:    直接破坏「语气词必须去掉」的铁律（tests/test_log_compressor.py）。
#:    一律用双字词（查看 / 读取 / 检查）。
_INTENT_VERBS_CN = (
    "修改", "重构", "修复", "测试", "添加", "删除", "实现", "更新",
    "创建", "优化", "排查", "调试", "部署", "验证", "集成", "替换",
    "调整", "改进", "重写", "迁移", "升级", "改造", "定位", "编译",
    "提交", "回滚", "覆盖", "拆分", "抽取", "清理", "梳理",
    # --- v4.0.1 补齐的动作动词 ---
    "读取", "查看", "检查", "追踪", "生成", "计算", "继续", "设计",
    "分析", "加载", "保存", "解析", "渲染", "绘制", "捕获", "模拟",
    "移动", "点击", "导航", "审查", "下载", "上传", "打印", "扫描",
    "搜索", "刷新", "重启", "获取", "编写", "撰写", "核对", "等待",
)

#: 英文意图关键词（正则匹配，带词边界）。
#:
#: ⚠️ **v4.0.1 扩容**：同 :data:`_INTENT_VERBS_CN` 的理由 ——
#:    Agent 的思考大量使用「Now let me <动词> …」句式，
#:    动词不在表里 → 整句被当废话丢掉 → 枢轴信号丢失。
#:
#: ⚠️ 正则是 ``\\b(?:verb)\\w*\\b``，会容忍变形
#:    （read/reading、check/checking），但也会命中
#:    ``ready``（read+dy）、``clearly``（clear+ly）这类同前缀词 ——
#:    这类误命中只会**多留一句**，不会丢信息，可接受。
_INTENT_VERBS_EN = (
    "fix", "refactor", "test", "add", "remove", "update", "implement",
    "create", "optimize", "optimise", "debug", "verify", "replace",
    "migrate", "upgrade", "rewrite", "integrate", "deploy", "build",
    "commit", "revert", "extract", "cleanup",
    # --- v4.0.1 补齐的动作动词 ---
    "write", "generate", "read", "see", "look", "check", "trace",
    "compute", "calculate", "clear", "rerun", "run", "continue", "get",
    "make", "design", "analyze", "analyse", "inspect", "load", "save",
    "parse", "render", "draw", "capture", "simulate", "move", "click",
    "navigate", "review", "download", "upload", "print", "scan",
    "search", "refresh", "restart", "fetch", "encode", "decode",
    "apply", "switch", "focus", "wait", "measure", "compare", "install",
    "toggle", "explain", "describe", "understand", "profile", "instrument",
    "find", "confirm", "ensure", "validate", "investigate", "explore",
    "examine", "produce", "emit", "define", "choose", "decide", "prefer",
    "avoid", "prevent", "solve", "answer", "respond", "retry", "repeat",
    "undo", "plan", "start", "identify", "detect", "diagnose", "summarize",
    "collect", "gather", "report", "publish", "release", "monitor",
)

#: 英文意图动词的正则（容忍 fix/fixes/fixed/fixing 这类变形）。
#:
#: ⚠️ **允许可选的 ``re`` 前缀**（v4.0.1）：``recompute`` / ``regenerate`` /
#:    ``recheck`` 这类**前缀粘连词**在 ``\\b`` 语义下匹配不到词根
#:    （``re`` 与词根之间没有词边界），而它们恰恰是
#:    「Agent 改变思路重算」的**转向信号**。
#:
#: ⚠️ 已知误命中：``re`` + ``get`` → ``regret``。只会**多留一句**，
#:    不会丢信息，代价可接受。
_INTENT_VERB_RE_EN = re.compile(
    r"\b(?:re)?(?:{})\w*\b".format("|".join(_INTENT_VERBS_EN)),
    re.IGNORECASE,
)

#: 语气词 / 寒暄标记 —— 命中的句子若不含意图动词，视为废话。
_FILLER_MARKERS = (
    "让我", "我先", "我来", "我需要", "我打算", "我想", "我认为",
    "我觉得", "好的", "好吧", "嗯", "呃", "啊", "哦", "明白了",
    "知道了", "确认一下", "确认下", "再次确认", "再确认",
    "似乎没问题", "看起来没问题", "没什么问题", "应该没问题",
    "稍等", "等等", "接下来", "然后呢", "先看看", "先看一下",
    "let me", "let's", "i need to", "i should", "i will",
    "i think", "i believe", "okay", "got it", "sure", "now,",
    "first,", "also,", "then,",
)

#: 句首语气词 —— 剥掉。
#:
#: ⚠️ 单字语气词（好 / 行 / 嗯……）**必须**跟着标点才算数，
#: 否则「好在」「行不通」会被砍掉半个词。
_LEADING_INTERJECTION = re.compile(
    r"^(?:好的|好吧"
    r"|(?:嗯|呃|额|啊|哦|唉|哈|好|行|是的|那么|其实|当然)\s*[，,、：:。.]"
    r")\s*[，,、：:。.\s]*"
)

#: 句首连接词 —— 剥掉。
_LEADING_CONNECTOR = re.compile(
    r"^(?:接下来|然后呢|然后|下一步|另外|此外|现在|此时|首先|其次|最后)"
    r"\s*[，,、：:。.\s]*"
)

#: 句首第一人称套话 —— 剥掉。
_LEADING_FIRST_PERSON = re.compile(
    r"^(?:让我|我先|我来|我需要|我打算|我想|我认为|我觉得|我要|我会|"
    r"我准备|我正在|我应该|我必须|我打算先)\s*"
)

#: 句尾语气助词 —— 剥掉（前提是剥完还剩得下内容）。
_TRAILING_PARTICLE = re.compile(r"[，,、]?\s*(?:了|吧|呢|哦|啊|嘛|哈)\s*$")

#: 句子切分 —— 中英混排都吃。
_SENTENCE_SPLIT = re.compile(
    r"[。！？；!?;\n]+|(?<=[.])\s+(?=[A-Z\"'(])"
)

#: 读取类工具（合并后叫「连续读取」）。
_READ_TOOLS = frozenset({"read", "view", "cat", "notebookread", "openfile"})

#: 编辑类工具（合并后叫「连续编辑」）。
_EDIT_TOOLS = frozenset(
    {"edit", "write", "multiedit", "apply_patch", "applypatch",
     "notebookedit", "str_replace", "str_replace_editor"}
)

#: 执行类工具（合并后叫「连续执行」）。
_EXEC_TOOLS = frozenset(
    {"bash", "shell", "run", "execute", "terminal", "command", "powershell"}
)

#: 检索类工具 —— 摘要优先展示「检索模式」而不是「检索路径」。
_PATTERN_TOOLS = frozenset(
    {"grep", "glob", "search", "codebase_search", "ripgrep", "rg"}
)

#: 可能承载「文件路径」的参数名。
_FILE_ARG_KEYS = (
    "file_path", "path", "notebook_path", "file", "filename",
    "target_file", "abs_path",
)

#: 可能承载「命令」的参数名。
_CMD_ARG_KEYS = (
    "command", "cmd", "command_line", "commandLine", "script", "shell",
)

#: 可能承载「检索模式」的参数名。
_PATTERN_ARG_KEYS = ("pattern", "query", "regex", "glob", "keyword")

#: 可能承载「描述」的参数名。
_DESC_ARG_KEYS = (
    "description", "prompt", "content", "text", "url", "message", "title",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class CompressionOptions:
    """压缩参数。

    :param goal: 任务目标；留空则从日志里的第一条 ``user_message`` 自动提取。
    :param max_chars: 输出字符预算；``0`` 表示不限。
    :param max_line_chars: 单行最大字符数。
    :param thinking_max_chars: ``thinking`` 意图提取后的最大字符数。
    :param merge_min_count: 连续同类动作达到多少条才合并。
    :param merge_preview: 合并后最多列出几个目标名。
    :param keep_text_events: 是否保留 ``text``（Agent 自述）事件。
    """

    goal: str = ""
    max_chars: int = 8000
    max_line_chars: int = 200
    thinking_max_chars: int = 120
    merge_min_count: int = 3
    merge_preview: int = 2
    keep_text_events: bool = True


@dataclass
class CompressedLog:
    """压缩结果。

    :param text: 压缩后的演进事件流文本。
    :param original_chars: 压缩前的字符数。
    :param compressed_chars: 压缩后的字符数。
    :param ratio: 压缩比（``compressed / original``，越小越好）。
    :param input_events: 读入的原始事件数。
    :param output_lines: 输出的行数。
    :param goal: 锚定出来的任务目标。
    :param counts: 各类型步骤的统计。
    """

    text: str = ""
    original_chars: int = 0
    compressed_chars: int = 0
    ratio: float = 0.0
    input_events: int = 0
    output_lines: int = 0
    goal: str = ""
    counts: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成字典（给 JSON 接口用）。

        :return: 结果字典。
        """
        return {
            "text": self.text,
            "original_chars": self.original_chars,
            "compressed_chars": self.compressed_chars,
            "ratio": round(self.ratio, 6),
            "input_events": self.input_events,
            "output_lines": self.output_lines,
            "goal": self.goal,
            "counts": dict(self.counts),
        }


@dataclass
class _Step:
    """内部步骤 —— 压缩流水线里的最小单元。

    :param kind: ``thinking`` / ``action`` / ``speech``。
    :param tool: 工具名。
    :param group_key: 合并分组键（同一组才合并）。
    :param target: 动作对象（文件路径 / 命令摘要）。
    :param text: 意图正文。
    :param status: ``成功`` / ``失败`` / ``执行中``。
    :param detail: 结果细节（如 ``43行``）。
    :param error: 是否失败。
    """

    kind: str = "action"
    tool: str = ""
    group_key: str = ""
    target: str = ""
    text: str = ""
    status: str = ""
    detail: str = ""
    error: bool = False


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------
def _split_sentences(text: str) -> List[str]:
    """把一段文字切成句子（中英混排）。

    :param text: 原始正文。
    :return: 句子列表（已去掉空白项）。
    """
    if not text:
        return []
    parts = _SENTENCE_SPLIT.split(text)
    return [part.strip() for part in parts if part and part.strip()]


def _has_intent_verb(sentence: str) -> bool:
    """判断句子是否含意图动词。

    ⚠️ 中文动词常常是**名词化**的 ——
    「怎么实现的」「现有的实现」里的「实现」并不是动作。
    所以动词**前面是「的」或后面是「的」**时都不算数。

    :param sentence: 单句正文。
    :return: 含动词返回 ``True``。
    """
    for verb in _INTENT_VERBS_CN:
        start = 0
        while True:
            index = sentence.find(verb, start)
            if index < 0:
                break
            prefix = sentence[index - 1] if index > 0 else ""
            suffix = sentence[index + len(verb)] if index + len(verb) < len(sentence) else ""
            if prefix != "的" and suffix != "的":
                return True
            start = index + 1
    return bool(_INTENT_VERB_RE_EN.search(sentence))


def _is_filler(sentence: str) -> bool:
    """判断一句是不是「语气词 / 反复确认」类的废话。

    ⚠️ 只在**不含意图动词**时才算废话 ——
    「接下来我要修改鉴权逻辑」里有语气词，但也有动作，不能丢。

    :param sentence: 单句正文。
    :return: 是废话返回 ``True``。
    """
    if len(sentence) < 4:
        return True
    if _has_intent_verb(sentence):
        return False
    lowered = sentence.lower()
    for marker in _FILLER_MARKERS:
        if marker in sentence or marker in lowered:
            return True
    return False


def _strip_filler(sentence: str) -> str:
    """剥掉句首句尾的语气词与第一人称套话。

    :param sentence: 单句正文。
    :return: 清理后的句子；清空了返回空串。
    """
    text = sentence.strip()
    changed = True
    while changed:
        changed = False
        for pattern in (
            _LEADING_INTERJECTION,
            _LEADING_CONNECTOR,
            _LEADING_FIRST_PERSON,
        ):
            stripped = pattern.sub("", text, count=1).strip()
            if stripped != text:
                text = stripped
                changed = True
    trimmed = _TRAILING_PARTICLE.sub("", text).strip()
    if len(trimmed) >= 4:
        text = trimmed
    return text.strip()


def _truncate(text: str, max_chars: int) -> str:
    """按字符数截断（超长时补省略号）。

    :param text: 原始文本。
    :param max_chars: 上限。
    :return: 截断后的文本。
    """
    limit = max(int(max_chars or 0), 1)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


#: 「结构化事实句」—— 冒号后面跟着实打实的内容。
_FACT_COLON_RE = re.compile(r"[:：]\s*\S")

#: 「结构化事实句」—— 以列表符号 / 序号开头。
_FACT_LIST_RE = re.compile(r"^\s*(?:[-*+·•]|\(?\d+[.)、])\s+\S")


def _is_fact_sentence(sentence: str) -> bool:
    """判断一句是不是「结构化事实句」。

    为什么需要它
    =============

    意图动词表覆盖的是「**动作**」——
    「生成素材」「读取文件」「计算角度」这类句子能被认出来。
    但 Agent 的思考里还有另一类**高信息密度**的句子：

    * 「Key facts: 1. **Webhook path**: engine → `_build_alert` …」
    * 「Requirements: 1. bearing = "前方" → offset angle in [67.5, 112.5]」
    * 「理由=正前方威胁，侧向平局取优先方向」

    这些句子**没有动词**（名词化的「路径 / 理由 / 要求」），
    却恰恰装着 L2 判定要看的**具体证据**。
    只按动词表筛，证据就被整段丢掉（v4.0.1 A/B 实测：
    窗口 21 的 webhook 调用链、窗口 38 的威胁方向理由都因此消失）。

    所以这里给一条**独立于动词表**的保留口径：
    带冒号实义、或以列表符号开头的句子，一律当成事实句留下。

    ⚠️ 这条口径**只放宽「留不留」**，不改变 :func:`extract_intent`
       的首句优先、语气词剥离等既有语义。

    :param sentence: 单句正文。
    :return: 是事实句返回 ``True``。
    """
    if _FACT_COLON_RE.search(sentence):
        return True
    return bool(_FACT_LIST_RE.match(sentence))


def extract_intent(text: str, *, max_chars: int = 120) -> str:
    """从一段长思考里提取核心意图。

    规则：取**首个有意义的句子**（首句）+ 后续**含意图动词**
    或**结构化事实句**的句子，其余的语气词、寒暄、反复确认一律丢掉。

    :param text: 原始 ``thinking`` 正文。
    :param max_chars: 提取结果的最大字符数。
    :return: 意图摘要；输入为空时返回空串。
    """
    if not text or not str(text).strip():
        return ""
    sentences = _split_sentences(str(text))
    kept: List[str] = []
    for sentence in sentences:
        # ⚠️ 先按**原句**判废话，再剥前缀 ——
        #    「先看看这个文件」剥掉「先」就认不出来了。
        if _is_filler(sentence):
            continue
        cleaned = _strip_filler(sentence)
        if not cleaned:
            continue
        if not kept:
            kept.append(cleaned)
            continue
        if _has_intent_verb(cleaned) or _is_fact_sentence(cleaned):
            kept.append(cleaned)
    if not kept:
        # 全是语气词 —— 没有信号。宁可整条丢掉，
        # 也不能把废话塞进本来就紧张的窗口里。
        return ""
    return _truncate("；".join(kept), max_chars)


def fallback_intent(text: str, *, max_chars: int = 120) -> str:
    """兜底意图 —— 给「整条被 :func:`extract_intent` 丢掉」的思考留条后路。

    为什么需要它
    =============

    :func:`extract_intent` 只认「意图动词表」里的动词
    （修改 / 修复 / 测试 / 集成 ……），
    所以「我先把旧构建产物清掉」这种**没有动词表命中**、
    却**确实说明了 Agent 想干什么**的话，会被整条丢掉。

    对**日志压缩**来说丢掉是合理的（它要的是高信噪比）；
    但对 **L2 漂移判定**来说这是**真实的信息损失** ——
    模型看不到「Agent 说它要清旧构建产物」这句话。

    所以这里给出一个**公开的兜底口径**：
    取首句、剥掉句首的语气词与第一人称套话，剩下的话原样保留。

    ⚠️ **本函数不改变 :func:`extract_intent` 的语义** ——
       它是并列的另一个口径，由调用方（引擎）自行决定用不用。

    :param text: 原始 ``thinking`` / ``text`` 正文。
    :param max_chars: 兜底结果的最大字符数。
    :return: 兜底意图；没有可用内容时返回空串。
    """
    if not text or not str(text).strip():
        return ""
    for sentence in _split_sentences(str(text)):
        cleaned = _strip_filler(sentence)
        if len(cleaned) >= 4:
            return _truncate(cleaned, max_chars)
    return ""


# ---------------------------------------------------------------------------
# 工具摘要
# ---------------------------------------------------------------------------
def _tool_family(tool: str) -> str:
    """把工具名归到「读 / 改 / 执行」三大族。

    :param tool: 工具名。
    :return: ``read`` / ``edit`` / ``exec`` / ``other``。
    """
    name = str(tool or "").strip().lower()
    if name in _READ_TOOLS:
        return "read"
    if name in _EDIT_TOOLS:
        return "edit"
    if name in _EXEC_TOOLS:
        return "exec"
    return "other"


def _first_arg(args: Dict[str, Any], keys: Iterable[str]) -> str:
    """按优先级取第一个非空的字符串参数。

    :param args: 工具参数字典。
    :param keys: 候选键名。
    :return: 参数值；取不到时返回空串。
    """
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            parts = [str(item) for item in value if isinstance(item, str)]
            if parts:
                return "\n".join(parts).strip()
    return ""


def _summarize_target(tool: str, args: Dict[str, Any]) -> str:
    """把工具参数压成一句「动了什么」。

    :param tool: 工具名。
    :param args: 工具参数。
    :return: 目标摘要（文件路径 / 命令 / 检索模式）。
    """
    if not isinstance(args, dict):
        return ""
    family = _tool_family(tool)
    lowered = str(tool or "").strip().lower()
    if lowered in _PATTERN_TOOLS:
        value = _first_arg(args, _PATTERN_ARG_KEYS)
        if not value:
            value = _first_arg(args, _FILE_ARG_KEYS)
    elif family in ("read", "edit"):
        value = _first_arg(args, _FILE_ARG_KEYS)
    elif family == "exec":
        value = _first_arg(args, _CMD_ARG_KEYS)
    else:
        value = _first_arg(args, _FILE_ARG_KEYS)
        if not value:
            value = _first_arg(args, _CMD_ARG_KEYS)
        if not value:
            value = _first_arg(args, _PATTERN_ARG_KEYS)
        if not value:
            value = _first_arg(args, _DESC_ARG_KEYS)
    if not value:
        return ""
    value = " ; ".join(part.strip() for part in value.splitlines() if part.strip())
    return _truncate(value, 120)


def _result_detail(text: str) -> str:
    """把工具回执正文压成一句细节描述（如 ``43行``）。

    ⚠️ **正文本身一律不保留** —— 那是几百行源码、几 KB 编译日志，
    留着就是浪费窗口。

    :param text: 回执正文。
    :return: 细节描述；正文为空时返回空串。
    """
    if not text or not str(text).strip():
        return ""
    body = str(text)
    lines = body.count("\n") + 1
    size = len(body.encode("utf-8", errors="ignore"))
    parts: List[str] = []
    if lines > 1:
        parts.append("{}行".format(lines))
    if size >= 256:
        parts.append("{:.1f}KB".format(size / 1024.0))
    return ", ".join(parts)


def _error_excerpt(text: str) -> str:
    """从失败回执里抠出第一行错误信息。

    :param text: 回执正文。
    :return: 错误摘要（最多 80 字）；没有则返回空串。
    """
    if not text or not str(text).strip():
        return ""
    first = str(text).strip().splitlines()[0].strip()
    return _truncate(first, 80)


# ---------------------------------------------------------------------------
# 事件归一化
# ---------------------------------------------------------------------------
def _tool_of(event: Event) -> Tuple[str, Dict[str, Any]]:
    """取出事件里的工具名与参数。

    :param event: 归一化事件。
    :return: ``(工具名, 参数字典)``。
    """
    tool = event.get("tool")
    if not isinstance(tool, dict):
        return "", {}
    name = str(tool.get("name") or "")
    args = tool.get("arguments")
    if not isinstance(args, dict):
        args = {}
    return name, args


def _normalize_events(
    events: Iterable[Event],
    options: CompressionOptions,
) -> Tuple[List[_Step], str]:
    """把归一化事件流压成一串步骤。

    职责：

    * ``tool_call`` → 一个「动作」步骤（等待回执）；
    * ``tool_result`` → 回填到对应的动作步骤上（正文只留元数据）；
    * ``thinking`` → 意图摘要；
    * ``text`` → 自述摘要（优先级最低）；
    * ``user_message`` → 任务目标。

    :param events: 归一化事件流。
    :param options: 压缩参数。
    :return: ``(步骤列表, 任务目标)``。
    """
    steps: List[_Step] = []
    pending: List[_Step] = []
    by_id: Dict[str, _Step] = {}
    goal = str(options.goal or "").strip()

    for event in events or ():
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "").strip().lower()
        text = str(event.get("text") or "")

        # --- 任务目标 ---
        if kind == "user_message":
            if not goal and text.strip():
                goal = _truncate(" ".join(text.split()), GOAL_MAX_CHARS)
            continue

        # --- 思考 ---
        if kind == "thinking":
            intent = extract_intent(text, max_chars=options.thinking_max_chars)
            if intent:
                steps.append(_Step(kind="thinking", text=intent))
            continue

        # --- Agent 自述 ---
        if kind == "text":
            if not options.keep_text_events:
                continue
            intent = extract_intent(text, max_chars=options.thinking_max_chars)
            if intent:
                steps.append(_Step(kind="speech", text=intent))
            continue

        # --- 工具调用 ---
        if kind == "tool_call":
            name, args = _tool_of(event)
            step = _Step(
                kind="action",
                tool=name or "工具",
                group_key=_tool_family(name),
                target=_summarize_target(name, args),
                status="执行中",
            )
            steps.append(step)
            pending.append(step)
            call_id = str(event.get("tool_use_id") or "")
            if call_id:
                by_id[call_id] = step
            continue

        # --- 工具回执 ---
        if kind == "tool_result":
            step = None
            call_id = str(event.get("tool_use_id") or "")
            if call_id and call_id in by_id:
                step = by_id.pop(call_id)
                pending = [item for item in pending if item is not step]
            else:
                while pending:
                    candidate = pending.pop(0)
                    if candidate.status == "执行中":
                        step = candidate
                        break
            if step is None:
                continue
            step.error = bool(event.get("is_error"))
            step.status = "失败" if step.error else "成功"
            step.detail = _result_detail(text)
            if step.error:
                excerpt = _error_excerpt(text)
                if excerpt:
                    step.detail = excerpt
            continue

        # --- 会话结束（result）之类的杂项：不进演进流 ---
        continue

    return steps, goal


# ---------------------------------------------------------------------------
# 合并同类动作
# ---------------------------------------------------------------------------
def _merge_label(tool: str) -> str:
    """合并后的动作标签。

    :param tool: 工具名。
    :return: 标签文本。
    """
    family = _tool_family(tool)
    if family == "read":
        return "连续读取"
    if family == "edit":
        return "连续编辑"
    if family == "exec":
        return "连续执行"
    return "连续{}".format(tool or "操作")


def _merge_unit(tool: str) -> str:
    """合并后计数的量词（含数词，如 ``个文件`` / ``条命令``）。

    :param tool: 工具名。
    :return: 量词短语。
    """
    family = _tool_family(tool)
    if family in ("read", "edit"):
        return "个文件"
    if family == "exec":
        return "条命令"
    return "项"


def _render_merged(
    group: List[_Step],
    preview: int,
) -> str:
    """把一整组同类动作渲染成一行。

    :param group: 同组步骤。
    :param preview: 最多列出几个目标名。
    :return: 一行文本。
    """
    tool = group[0].tool if group else ""
    label = _merge_label(tool)
    unit = _merge_unit(tool)
    targets = [step.target for step in group if step.target]
    text = "[{}]".format(label)
    if targets:
        head = ", ".join(targets[: max(preview, 1)])
        if len(targets) > preview:
            head += " 等 {} {}".format(len(targets), unit)
        text += " " + head
    failed = sum(1 for step in group if step.error)
    succeeded = sum(1 for step in group if step.status == "成功")
    running = sum(1 for step in group if step.status == "执行中")
    if failed:
        text += " ({} 成功, {} 失败)".format(succeeded, failed)
    elif running:
        text += " ({} 进行中)".format(running)
    return text


def _merge_steps(
    steps: List[_Step],
    options: CompressionOptions,
) -> List[_Step]:
    """合并连续的同类动作。

    :param steps: 步骤列表。
    :param options: 压缩参数。
    :return: 合并后的步骤列表。
    """
    merged: List[_Step] = []
    group: List[_Step] = []

    def flush() -> None:
        if not group:
            return
        if len(group) >= max(int(options.merge_min_count or 1), 2):
            merged.append(
                _Step(
                    kind="merged",
                    tool=group[0].tool,
                    group_key=group[0].group_key,
                    text=_render_merged(group, options.merge_preview),
                )
            )
        else:
            merged.extend(group)
        group.clear()

    for step in steps:
        if step.kind != "action":
            flush()
            merged.append(step)
            continue
        if group and group[0].group_key == step.group_key:
            group.append(step)
            continue
        flush()
        group.append(step)
    flush()
    return merged


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _render_step(step: _Step) -> str:
    """把一个步骤渲染成一行（不含序号）。

    :param step: 步骤。
    :return: 一行文本。
    """
    if step.kind == "merged":
        return step.text
    if step.kind == "thinking":
        return "[思考] {}".format(step.text)
    if step.kind == "speech":
        return "[说明] {}".format(step.text)
    parts = ["[{}]".format(step.tool or "工具")]
    if step.target:
        parts.append(step.target)
    bits: List[str] = []
    if step.status:
        bits.append(step.status)
    if step.detail:
        bits.append(step.detail)
    if bits:
        parts.append("({})".format(", ".join(bits)))
    return " ".join(parts)


def _current_action(steps: List[_Step]) -> str:
    """推断「当前正在干什么」。

    :param steps: 步骤列表。
    :return: 一句话描述。
    """
    for step in reversed(steps):
        if step.kind == "action":
            return _render_step(step)
        if step.kind == "merged":
            return step.text
    for step in reversed(steps):
        if step.kind in ("thinking", "speech"):
            return step.text
    return "（无进行中的动作）"


def _fit_body(
    body: List[str],
    budget: int,
    max_line_chars: int,
) -> List[str]:
    """把正文压进字符预算。

    降级顺序（从温和到强硬）：

    1. 逐行截断到 ``max_line_chars``；
    2. 丢掉中间的「自述」行；
    3. 丢掉中间的「思考」行；
    4. 丢掉中间的动作行（保住头尾的逻辑链条）。

    :param body: 正文行。
    :param budget: 预算字符数（不含锚定行）。
    :param max_line_chars: 单行上限。
    :return: 调整后的正文行。
    """

    def total(lines: List[str]) -> int:
        return sum(len(line) + 1 for line in lines)

    if budget <= 0 or total(body) <= budget:
        return body

    shortened = [_truncate(line, max_line_chars) for line in body]
    if total(shortened) <= budget:
        return shortened

    # --- 丢自述 ---
    # ⚠️ 用**子串**匹配而不是前缀 —— 渲染后的行带序号
    #    （"[3] [说明] …"），前缀匹配永远命中不了。
    trimmed = [line for line in shortened if "[说明]" not in line]
    if total(trimmed) <= budget:
        return trimmed

    # --- 丢思考（保住首个与末个思考，它们承接上下文）---
    thinking_indexes = [
        index
        for index, line in enumerate(trimmed)
        if "[思考]" in line
    ]
    if thinking_indexes:
        keep = {thinking_indexes[0], thinking_indexes[-1]}
        trimmed = [
            line
            for index, line in enumerate(trimmed)
            if "[思考]" not in line or index in keep
        ]
        if total(trimmed) <= budget:
            return trimmed

    # --- 丢中间的动作行（保住头 3 与尾 3）---
    if len(trimmed) > 6:
        head = trimmed[:3]
        tail = trimmed[-3:]
        middle = trimmed[3:-3]
        while middle and total(head + tail + middle) > budget:
            middle.pop(len(middle) // 2)
        trimmed = head + middle + tail
    return trimmed


def render_stream(
    steps: List[_Step],
    goal: str = "",
    *,
    options: Optional[CompressionOptions] = None,
) -> str:
    """把步骤渲染成带锚定行的演进事件流。

    :param steps: 步骤列表。
    :param goal: 任务目标。
    :param options: 压缩参数。
    :return: 完整文本。
    """
    opts = options or CompressionOptions()
    # ⚠️ **v4.0.1 修复**：目标文本必须**压成一行**。
    #    调用方传进来的目标常常带换行（生产里 goal 是用户原始任务说明），
    #    原实现直接拼进 ``[Task Goal]:`` 行，于是锚定行被撑成多行 ——
    #    引擎侧 :func:`src.core.engine._strip_compressed_goal` 只剥**首行**，
    #    目标的正文（几行需求清单）就**漏进演进流**，
    #    既挤占预算又让模型看到同一段话两遍。
    goal_text = " ".join(str(goal or "").split())
    goal_text = goal_text or "（未记录任务目标）"
    goal_text = _truncate(goal_text, GOAL_MAX_CHARS)

    body = []
    for index, step in enumerate(steps, start=1):
        line = _render_step(step)
        line = _truncate(line, opts.max_line_chars)
        body.append("[{}] {}".format(index, line))

    current = _current_action(steps)
    current = _truncate(current, opts.max_line_chars)

    head = "[Task Goal]: {}".format(goal_text)
    tail = "[Current Action]: {}".format(current)

    budget = int(opts.max_chars or 0)
    if budget > 0:
        body = _fit_body(body, budget - len(head) - len(tail) - 2, opts.max_line_chars)

    lines = [head]
    lines.extend(body)
    lines.append(tail)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def compress_events(
    events: Iterable[Event],
    *,
    options: Optional[CompressionOptions] = None,
) -> CompressedLog:
    """把归一化事件流压成演进事件流。

    :param events: 归一化事件流（适配器产出的事件字典）。
    :param options: 压缩参数。
    :return: 压缩结果。
    """
    opts = options or CompressionOptions()
    materialised = [event for event in (events or ()) if isinstance(event, dict)]
    original_chars = 0
    for event in materialised:
        try:
            original_chars += len(
                json.dumps(event, ensure_ascii=False, default=str)
            )
        except (TypeError, ValueError):  # pragma: no cover —— 兜底
            original_chars += len(str(event))

    try:
        steps, goal = _normalize_events(materialised, opts)
        steps = _merge_steps(steps, opts)
        text = render_stream(steps, goal, options=opts)
    except Exception as exc:  # noqa: BLE001 —— 审查侧绝不中断（红线 R5）
        logger.warning("[log_compressor] 压缩事件流失败，已降级：%s", exc)
        return CompressedLog(
            text="[Task Goal]: （未记录任务目标）\n[Current Action]: （无进行中的动作）",
            original_chars=original_chars,
            compressed_chars=0,
            ratio=0.0,
            input_events=len(materialised),
        )

    counts: Dict[str, int] = {}
    for step in steps:
        counts[step.kind] = counts.get(step.kind, 0) + 1
    compressed_chars = len(text)
    return CompressedLog(
        text=text,
        original_chars=original_chars,
        compressed_chars=compressed_chars,
        ratio=(compressed_chars / original_chars) if original_chars else 0.0,
        input_events=len(materialised),
        output_lines=len(text.splitlines()),
        goal=goal,
        counts=counts,
    )


def _parse_line(
    line: str,
    mcode: McodeAdapter,
    claude: ClaudeAdapter,
    deltas: Optional[Dict[str, List[str]]] = None,
) -> List[Event]:
    """把一行 JSON 解析成归一化事件（自动识别协议）。

    ⚠️ mcode v0.6 会把一段思考**切成碎片**反复推送
    （``item.contentDelta``），单看一行只有「让我」两个字。
    这里按 ``item.id`` 把碎片**攒起来**，等该 item 完成时合成一条完整的
    ``thinking`` 事件 —— 否则压缩出来的事件流全是半截句子。

    :param line: 一行原始文本。
    :param mcode: mcode 适配器（长生命周期，v0.6 协议要靠它去重）。
    :param claude: Claude 适配器。
    :param deltas: 碎片缓冲区（跨行累积）。
    :return: 事件列表；坏行返回空列表。
    """
    stripped = line.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []

    payload_type = str(payload.get("type") or "").strip().lower()
    try:
        # --- mcode v0.6 事件溯源协议 ---
        if "item" in payload or payload_type.startswith("item."):
            return _parse_item_line(payload, mcode, deltas)
        # --- Claude Code 消息容器协议 ---
        if payload_type in ("assistant", "user", "human", "system", "result"):
            return claude.parse_payload(payload)
        # --- 平铺形态：mcode 适配器认得 ---
        events = mcode.parse_payload(payload)
        if events:
            return events
        return claude.parse_payload(payload)
    except Exception as exc:  # noqa: BLE001 —— 坏行只跳过，不断流
        logger.warning("[log_compressor] 解析一行失败，已跳过：%s", exc)
        return []


def _parse_item_line(
    payload: Dict[str, Any],
    mcode: McodeAdapter,
    deltas: Optional[Dict[str, List[str]]],
) -> List[Event]:
    """解析 mcode v0.6 的 ``item.*`` 行（含碎片累加）。

    :param payload: 整行载荷。
    :param mcode: mcode 适配器。
    :param deltas: 碎片缓冲区。
    :return: 事件列表。
    """
    item = payload.get("item")
    if not isinstance(item, dict):
        return mcode.parse_payload(payload)

    item_id = str(item.get("id") or "")
    item_type = str(item.get("type") or "").strip().lower()
    delta = item.get("contentDelta")

    # --- 碎片：先攒着，不出事件 ---
    if isinstance(delta, str) and delta:
        if deltas is not None and item_id:
            deltas.setdefault(item_id, []).append(delta)
        return []

    events = mcode.parse_payload(payload)

    # --- 该 item 收尾：把攒下来的碎片补成一条完整思考 ---
    if deltas is not None and item_id and item_id in deltas:
        merged = "".join(deltas.pop(item_id))
        if merged and item_type in ("reasoning", "thinking", "thought"):
            has_thinking = any(
                str(event.get("type") or "") == "thinking"
                for event in events
                if isinstance(event, dict)
            )
            if not has_thinking:
                events.insert(0, {"type": "thinking", "text": merged})
    return events


def compress_jsonl(
    lines: Iterable[str],
    *,
    options: Optional[CompressionOptions] = None,
) -> CompressedLog:
    """把 JSONL 日志压成演进事件流。

    :param lines: JSONL 原始行（可以来自文件、管道或内存）。
    :param options: 压缩参数。
    :return: 压缩结果。
    """
    opts = options or CompressionOptions()
    mcode = McodeAdapter()
    claude = ClaudeAdapter()
    events: List[Event] = []
    original_chars = 0
    deltas: Dict[str, List[str]] = {}
    for line in lines or ():
        if line is None:
            continue
        raw = str(line)
        original_chars += len(raw)
        events.extend(_parse_line(raw, mcode, claude, deltas))

    # --- 收尾：会话被截断时，攒下的碎片也得落地 ---
    for item_id, parts in list(deltas.items()):
        merged = "".join(parts).strip()
        if merged:
            events.append({"type": "thinking", "text": merged})

    result = compress_events(events, options=opts)
    result.original_chars = original_chars or result.original_chars
    if result.original_chars:
        result.ratio = result.compressed_chars / result.original_chars
    return result


def compress_file(
    path: str,
    *,
    options: Optional[CompressionOptions] = None,
) -> CompressedLog:
    """把一个 JSONL 日志文件压成演进事件流。

    ⚠️ **只读** —— 绝不写入、截断、删除被监控文件（红线 R1）。

    :param path: 日志文件路径。
    :param options: 压缩参数。
    :return: 压缩结果；文件读不到时返回空结果。
    """
    target = os.fspath(path)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            return compress_jsonl(handle, options=options)
    except OSError as exc:
        logger.warning("[log_compressor] 读取日志失败：%s", exc)
        return CompressedLog(
            text="[Task Goal]: （未记录任务目标）\n[Current Action]: （无进行中的动作）"
        )
