"""L2 输出解析 —— 容错阶梯。

为什么需要它
============
本地小模型的 **JSON 格式不稳定** 是实测结论，不是猜测：
旧项目 ``dsh-plain-approval`` 在本地 Ollama + qwen2.5:7b 下，
``output_type=<BaseModel>``（走 tool call）**10 次失败 6 次（60%）**，
失败形态是模型把 tool call 当正文吐出来，报
``Invalid JSON: expected ident at line 1 column 2``。

因此哨兵不追求「模型必须吐出完美 JSON」，而是**能从各种脏输出里把结论捞出来**。

解析阶梯
========
1. ``strip_noise``          —— 去代码围栏 / BOM / 首尾空白
2. ``extract_json_object``  —— 找出第一个**配平**的 ``{...}``
   （正确跳过字符串字面量内部的括号与转义）
3. ``remove_trailing_commas`` —— 去掉 JSON 尾逗号
4. ``labelled_text``        —— 标注式文本兜底
   （``title: xxx`` / ``why: xxx`` / ``touch: xxx`` / ``outcome: xxx``）
5. 失败 —— 返回 ``ok=False``，由调用方走降级

阶梯设计提取自 ``dsh-plain-approval/src/parse.js:69-92``，
按哨兵的需要扩展了「配平括号」与「尾逗号」两步。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = [
    "ParseResult",
    "extract_json_object",
    "extract_last_json_object",
    "parse_drift_payload",
    "parse_explain_payload",
    "parse_labelled_text",
    "remove_trailing_commas",
    "strip_noise",
    "strip_thinking",
]


#: 我们关心的字段（与 ``prompts.EXPLAIN_OUTPUT_FIELDS`` 对齐）。
_FIELDS = ("title", "why", "touch", "outcome")

#: 代码围栏：```json ... ``` 或 ``` ... ```
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*([\s\S]*?)\s*```")

#: 思考段标签：``<think>…</think>`` / ``<thinking>…</thinking>`` / ``<reasoning>…``。
#:
#: v3.2.1 实机踩到：NInfer（Bonsai2-27B）是**推理模型**，
#: 思考过程会带着标签混在正文里 —— 它**不能**进看板的告警文案。
_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

#: 没闭合的思考段（模型被 ``max_tokens`` 截断时很常见）。
_THINK_OPEN_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)

#: 标注式文本：``title: xxx``（中英文冒号都认；允许 markdown 强调与列表符号）
_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*>]\s*)?[*_`]*\s*"
    r"(title|why|touch|outcome)\s*[*_`]*\s*[:：]\s*(.+?)\s*$"
)

#: 漂移判定专用的标注式文本：``drift: true`` / ``reason: …``
_DRIFT_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*>]\s*)?[*_`]*\s*"
    r"(drift|reason)\s*[*_`]*\s*[:：]\s*(.+?)\s*$"
)

#: 级联三值判定专用的标注式文本：``verdict: DRIFT`` / ``reason: …``
#:
#: ⚠️ 刻意**单独**一条 —— 把 ``verdict`` 塞进漂移那条正则里，
#:    会让漂移判定的标注式兜底**顺手**接受三值文本，
#:    契约就乱了。
#:
#: 中文标签（``判定：`` / ``理由：``）也认 ——
#: 模型用中文回答是常事，只认英文标签等于白写这一级兜底。
_CASCADE_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*>]\s*)?[*_`]*\s*"
    r"(verdict|判定|结论|reason|理由)\s*[*_`]*\s*[:：]\s*(.+?)\s*$"
)

#: 标注式文本里的**中文键** → 规范键名。
_CASCADE_LABEL_ALIASES = {
    "判定": "verdict",
    "结论": "verdict",
    "理由": "reason",
}

#: 布尔值的各种写法（模型不一定会给真正的 JSON 布尔）。
_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "是", "有", "对"})
_FALSE_WORDS = frozenset({"false", "no", "n", "0", "否", "无", "不对"})


@dataclass(frozen=True)
class ParseResult:
    """一次解析的结果。

    :param ok:   是否成功捞出了结论。
    :param data: 解析出的字段字典（失败时为空）。
    :param raw:  模型的原始输出（始终保留，便于排障）。
    :param reason: 失败原因（成功时为空串）。
    :param mode: 命中了阶梯的哪一级：``json`` / ``labelled`` / ``failed``。
    """

    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    reason: str = ""
    mode: str = "failed"

    def get(self, key: str, default: str = "") -> str:
        """取一个字段（永远返回字符串）。"""
        value = self.data.get(key, default)
        return value if isinstance(value, str) else default


def strip_noise(raw: str) -> str:
    """去掉代码围栏、BOM 与首尾空白。"""
    if not raw:
        return ""
    text = raw.replace("\ufeff", "").strip()
    # 去掉整段代码围栏；模型有时在围栏外还写了一句寒暄。
    match = _FENCE_RE.search(text)
    if match:
        inner = match.group(1).strip()
        if inner:
            text = inner
    return text.strip()


def extract_json_object(text: str) -> Optional[str]:
    """找出第一个**配平**的 JSON 对象字面量。

    与 ``text.find("{")`` 的区别：后者会把字符串字面量里的 ``{`` 当成起点，
    也无法处理嵌套。本实现正确跳过字符串与转义。

    :param text: 待搜索文本。
    :return: JSON 对象字面量；找不到返回 ``None``。
    """
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def strip_thinking(raw: str) -> str:
    """把模型的**思考过程**从正文里剥掉。

    为什么必须有这一步
    ==================

    v3.2.1 实机踩到：NInfer（Bonsai2-27B）是**推理模型**，
    它的思考过程会带着 ``<think>…</think>`` 标签，
    或者干脆整个塞在 ``reasoning_content`` 字段里。

    思考过程**绝不能**进看板的告警文案 ——
    看板上要的是「一句话说清这条命令为什么危险」，
    不是模型的三千字内心戏。

    本函数处理两种形态：

    1. 正文里**带标签**的思考段 → 整段删掉；
    2. 没闭合的思考段（模型被 ``max_tokens`` 截断）→ 删到结尾。

    :param raw: 模型原始输出。
    :return: 去掉思考过程之后的正文（可能为空串）。
    """
    if not raw:
        return ""
    text = _THINK_BLOCK_RE.sub("", raw)
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


def extract_last_json_object(text: str) -> Optional[str]:
    """找出文本里**最后一个**配平的 JSON 对象字面量。

    与 :func:`extract_json_object` 的区别：那个取**第一个**，
    这个取**最后一个** —— 推理模型常常在正文里先聊、再给结论，
    结论（那段 JSON）在**末尾**。

    :param text: 待搜索文本。
    :return: JSON 对象字面量；找不到返回 ``None``。
    """
    if not text:
        return None
    found: Optional[str] = None
    position = 0
    while True:
        index = text.find("{", position)
        if index < 0:
            break
        candidate = extract_json_object(text[index:])
        if candidate:
            found = candidate
            position = index + len(candidate)
        else:
            position = index + 1
    return found


def remove_trailing_commas(text: str) -> str:
    """去掉 JSON 尾逗号（``{"a":1,}`` → ``{"a":1}``）。"""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def parse_labelled_text(text: str) -> Dict[str, Any]:
    """标注式文本兜底解析。

    从 ``title: xxx`` 这类行里把字段捞出来。模型在 JSON 崩掉时经常会
    退化成这种格式，捞出来总比丢掉强。
    """
    data: Dict[str, Any] = {}
    for match in _LABEL_RE.finditer(text):
        key = match.group(1).lower()
        value = match.group(2).strip()
        # 去掉可能的 markdown 强调符号与首尾引号
        value = value.strip("*_` ").strip('"').strip("'").strip()
        if key not in data and value:
            data[key] = value
    return data


def parse_explain_payload(raw: str) -> ParseResult:
    """把模型的原始输出解析成结构化结论。

    :param raw: 模型原始输出。
    :return: :class:`ParseResult`；``ok=False`` 表示应走降级。
    """
    text = strip_noise(raw or "")
    if not text:
        return ParseResult(ok=False, raw=raw or "", reason="模型输出为空")

    # --- 阶梯 2/3：配平 JSON + 去尾逗号 ---
    candidate = extract_json_object(text)
    if candidate:
        for attempt in (candidate, remove_trailing_commas(candidate)):
            try:
                data = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            cleaned = {
                key: value
                for key, value in data.items()
                if isinstance(value, (str, list, dict, int, float, bool))
            }
            if isinstance(cleaned.get("title"), str) and cleaned["title"].strip():
                return ParseResult(
                    ok=True,
                    data=cleaned,
                    raw=raw or "",
                    mode="json",
                )

    # --- 阶梯 4：标注式文本兜底 ---
    labelled = parse_labelled_text(text)
    if isinstance(labelled.get("title"), str) and labelled["title"].strip():
        return ParseResult(ok=True, data=labelled, raw=raw or "", mode="labelled")

    return ParseResult(
        ok=False,
        raw=raw or "",
        reason="模型输出中找不到可识别的结论（既不是 JSON，也不是标注式文本）",
    )


def _coerce_bool(value: Any) -> Optional[bool]:
    """把各种形态的「是 / 否」归一成布尔值。

    模型给出的 ``drift`` 可能是：
    真布尔 ``true``、字符串 ``"true"``、中文 ``"是"``、数字 ``1``……
    全都认 —— 认出来就比丢掉强。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().strip("\"'").lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return None


def parse_drift_payload(raw: str) -> ParseResult:
    """把模型输出解析成「是否漂移」的结论。

    与 :func:`parse_explain_payload` 用同一套容错阶梯，但字段契约不同
    （``drift`` / ``reason``，且 ``drift`` 是**布尔**）。

    :param raw: 模型原始输出。
    :return: :class:`ParseResult`；``data["drift"]`` 是布尔值。
        ``ok=False`` 表示应走降级（**不判定为漂移** —— 缺失绝不等同于有问题）。
    """
    text = strip_noise(raw or "")
    if not text:
        return ParseResult(ok=False, raw=raw or "", reason="模型输出为空")

    # --- 阶梯 2/3：配平 JSON + 去尾逗号 ---
    candidate = extract_json_object(text)
    if candidate:
        for attempt in (candidate, remove_trailing_commas(candidate)):
            try:
                data = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            drift = _coerce_bool(data.get("drift"))
            if drift is None:
                continue
            reason = data.get("reason")
            return ParseResult(
                ok=True,
                data={
                    "drift": drift,
                    "reason": reason if isinstance(reason, str) else "",
                },
                raw=raw or "",
                mode="json",
            )

    # --- 阶梯 4：标注式文本兜底 ---
    labelled: Dict[str, Any] = {}
    for match in _DRIFT_LABEL_RE.finditer(text):
        key = match.group(1).lower()
        value = match.group(2).strip().strip("*_` ").strip('"').strip("'").strip()
        if key not in labelled and value:
            labelled[key] = value
    drift = _coerce_bool(labelled.get("drift"))
    if drift is not None:
        return ParseResult(
            ok=True,
            data={"drift": drift, "reason": str(labelled.get("reason") or "")},
            raw=raw or "",
            mode="labelled",
        )

    return ParseResult(
        ok=False,
        raw=raw or "",
        reason="模型输出中找不到可识别的漂移结论（既不是 JSON，也不是标注式文本）",
    )


#: 级联三值判定的**同义写法** → 规范值（v3.5.0）。
#:
#: ⚠️ 模型不一定照着写 ``ON_TRACK`` —— 它可能写 ``on track``、
#:    ``正常``、``没问题``。所以这里收一批同义写法。
_CASCADE_ALIASES = {
    "on_track": "ON_TRACK",
    "ontrack": "ON_TRACK",
    "on track": "ON_TRACK",
    "ok": "ON_TRACK",
    "fine": "ON_TRACK",
    "正常": "ON_TRACK",
    "没问题": "ON_TRACK",
    "跟踪": "ON_TRACK",
    "drift": "DRIFT",
    "drifted": "DRIFT",
    "偏离": "DRIFT",
    "漂移": "DRIFT",
    "跑偏": "DRIFT",
    "dangerous": "DANGEROUS",
    "danger": "DANGEROUS",
    "危险": "DANGEROUS",
    "严重": "DANGEROUS",
}


def normalize_cascade_verdict(value: Any) -> str:
    """把模型给的三值判定规范化。

    :param value: 模型给的原始值。
    :return: ``ON_TRACK`` / ``DRIFT`` / ``DANGEROUS``；
        认不出来就是**空串**（= 不可信，走降级）。
    """
    text = str(value or "").strip().strip("*_` ").strip('"').strip("'")
    if not text:
        return ""
    upper = text.upper().replace("-", "_").replace(" ", "_")
    if upper in ("ON_TRACK", "DRIFT", "DANGEROUS"):
        return upper
    return _CASCADE_ALIASES.get(text.lower(), "")


def parse_cascade_payload(raw: str) -> ParseResult:
    """把模型输出解析成**三值**判定（v3.5.0）。

    与 :func:`parse_drift_payload` 用同一套容错阶梯，但字段契约是
    ``verdict``（三值字符串）+ ``reason``。

    ⚠️ 认不出来就 ``ok=False`` —— 调用方据此**降级**，
    绝不把「认不出」当成 ``ON_TRACK``（那会让真问题被静默吞掉）。

    :param raw: 模型原始输出。
    :return: :class:`ParseResult`；``data["verdict"]`` 是三值之一。
    """
    text = strip_noise(raw or "")
    if not text:
        return ParseResult(ok=False, raw=raw or "", reason="模型输出为空")

    # --- 阶梯 2/3：配平 JSON + 去尾逗号 ---
    candidate = extract_json_object(text)
    if candidate:
        for attempt in (candidate, remove_trailing_commas(candidate)):
            try:
                data = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            verdict = normalize_cascade_verdict(data.get("verdict"))
            if not verdict:
                continue
            reason = data.get("reason")
            return ParseResult(
                ok=True,
                data={
                    "verdict": verdict,
                    "reason": reason if isinstance(reason, str) else "",
                },
                raw=raw or "",
                mode="json",
            )

    # --- 阶梯 4：标注式文本兜底 ---
    labelled: Dict[str, Any] = {}
    for match in _CASCADE_LABEL_RE.finditer(text):
        key = match.group(1).lower()
        key = _CASCADE_LABEL_ALIASES.get(key, key)
        value = match.group(2).strip().strip("*_` ").strip('"').strip("'").strip()
        if key not in labelled and value:
            labelled[key] = value
    verdict = normalize_cascade_verdict(labelled.get("verdict"))
    if verdict:
        return ParseResult(
            ok=True,
            data={"verdict": verdict, "reason": str(labelled.get("reason") or "")},
            raw=raw or "",
            mode="labelled",
        )

    return ParseResult(
        ok=False,
        raw=raw or "",
        reason="模型输出中找不到可识别的三值判定（既不是 JSON，也不是标注式文本）",
    )
