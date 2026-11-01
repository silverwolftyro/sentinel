"""配置加载 —— 哨兵最底层的公共工具。

这是一个**叶子模块**，不依赖 `src` 下的任何其他模块，因此规则层与核心层
都可以安全地导入它，不会产生层级倒置或循环依赖。

默认配置路径：``<项目根>/config/config.yaml``。

v2.3.0 起本模块同时负责**配置写回**：在保留用户注释的前提下原地改值。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import yaml

__all__ = [
    "APP_DIR",
    "DEFAULT_CONFIG_PATH",
    "PROJECT_ROOT",
    "default_config_candidates",
    "ensure_config_defaults",
    "load_config",
    "read_config_text",
    "save_config",
    "update_config_text",
]

logger = logging.getLogger(__name__)

#: 项目根目录（``src/`` 的上一级）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 默认主配置路径。
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")

#: 打包成 exe 后的「程序所在目录」——
#:
#: 单文件程序没有「项目根」的概念（那在临时解包目录里），
#: 用户真正能编辑、能长期保存的地方只有 **exe 旁边**。
#: 所以打包运行时，配置优先从这儿找。
APP_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else ""
)


def default_config_candidates() -> List[str]:
    """默认配置文件的候选路径（**按优先级从高到低**）。

    1. ``<exe 所在目录>/config/config.yaml`` —— 打包运行时用户改得动的那个；
    2. ``<项目根>/config/config.yaml`` —— 源码运行时的常规位置；
    3. ``<exe 所在目录>/config/config.example.yaml`` —— 用户自己放的模板；
    4. ``<解包目录>/config/config.example.yaml`` —— 打包时随包带的模板
       （源码运行时就是 ``<项目根>/config/config.example.yaml``）。

    :return: 候选路径列表（可能为空）。
    """
    candidates: List[str] = []
    if APP_DIR:
        candidates.append(os.path.join(APP_DIR, "config", "config.yaml"))
    candidates.append(DEFAULT_CONFIG_PATH)
    if APP_DIR:
        candidates.append(
            os.path.join(APP_DIR, "config", "config.example.yaml")
        )
    bundle = getattr(sys, "_MEIPASS", "") or ""
    if bundle:
        candidates.append(
            os.path.join(bundle, "config", "config.example.yaml")
        )
    return candidates


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载 YAML 配置。

    文件不存在、YAML 非法、顶层不是字典时，一律返回空字典而**不抛异常** ——
    配置问题不应让哨兵启动失败（红线 R5：旁路失败）。

    :param path: 配置文件路径；为 ``None`` 时按
        :func:`default_config_candidates` 的顺序找。
    :return: 配置字典，可能为空。
    """
    candidates = [path] if path else default_config_candidates()
    for config_path in candidates:
        if not config_path or not os.path.exists(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            return data
    return {}


# ---------------------------------------------------------------------------
# 配置写回（v2.3.0）
# ---------------------------------------------------------------------------
#
# Web 端的独立设置页需要把用户的改动写回 config.yaml 并触发热重载，
# 但 config/config.example.yaml 里写满了中文注释 —— 那是用户唯一的参考。
# 用 yaml.safe_dump() 整体重写会把这些注释**全部抹掉**，属于不可接受的回归。
#
# 所以这里做的是**外科手术式的原地改值**：
# 只替换目标键的**值**，其余字节（注释、空行、缩进风格、换行风格）
# 原样保留。
#
# 本节**不依赖 yaml** —— 纯文本手术，连解析都只做到「找键名 + 改值」
# 这一层；引号、注释、缩进这些细节一律原样带回。

#: yaml 路径里「键名[下标]」的匹配式（只支持单段下标）。
_PATH_SEGMENT_RE = re.compile(r"^([^\[\]]+)(?:\[(\d+)\])?$")

#: UTF-8 BOM —— 读配置时要剥掉，写回时原样带上。
_BOM = "\ufeff"


class _LineInfo(NamedTuple):
    """一行 YAML 的浅解析结果。

    只解析到「键名 / 值」这一层：引号内容、行尾注释、缩进风格
    全部保持原样，方便后面做**原地**替换。
    """

    no: int          # 行号（0 起）
    indent: int      # 缩进列数（制表符按 4 列计）
    kind: str        # "map" / "seq" / "blank" / "comment" / "scalar"
    key: str         # 映射键名；序列项内联映射时是该映射的键名
    value: str       # 值原文（未做 YAML 反序列化）
    value_start: int  # 值在原始行中的起始下标（无行内值时为 -1）
    value_end: int    # 值在原始行中的结束下标
    colon: int        # 键与值之间冒号的下标（无则为 -1）


class _Scope(NamedTuple):
    """一层块结构里的候选项（都是行号）。"""

    map_keys: List[int]   # 本层可直接匹配的映射键
    seq_items: List[int]  # 本层的序列项


def _indent_of(raw: str) -> int:
    """计算一行的缩进列数（制表符按 4 列计）。"""
    width = 0
    for ch in raw:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4
        else:
            break
    return width


def _skip_space(raw: str, pos: int) -> int:
    """跳过 ``raw`` 中 ``pos`` 处的空白字符。"""
    while pos < len(raw) and raw[pos] in " \t\r":
        pos += 1
    return pos


def _find_colon(content: str, offset: int) -> int:
    """找出 ``content`` 里作为「键: 值」分隔符的冒号。

    引号内、流式集合 ``[]`` / ``{}`` 内的冒号不算数；
    行尾注释（``#`` 前有空白）之后的内容直接忽略。

    :param content: 待扫描的片段（已去掉缩进）。
    :param offset: 该片段在原始行中的起始下标。
    :return: 冒号在原始行中的下标；找不到返回 ``-1``。
    """
    quote = ""
    depth = 0
    index = 0
    size = len(content)
    while index < size:
        ch = content[index]
        if quote:
            if ch == "\\" and quote == '"':
                index += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ('"', "'"):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "#" and (index == 0 or content[index - 1] in " \t"):
            break
        elif (
            ch == ":"
            and depth == 0
            and (index + 1 >= size or content[index + 1] in " \t\r")
        ):
            return offset + index
        index += 1
    return -1


def _scan_value(raw: str, start: int) -> Tuple[int, int]:
    """扫描出 ``raw`` 中从 ``start`` 开始的值文本的 ``[begin, end)`` 区间。

    值到行尾注释为止；结尾的空白（含 ``\\r``）不算值的一部分。
    """
    quote = ""
    depth = 0
    index = start
    size = len(raw)
    while index < size:
        ch = raw[index]
        if quote:
            if ch == "\\" and quote == '"':
                index += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ('"', "'"):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif (
            ch == "#"
            and depth == 0
            and (index == 0 or raw[index - 1] in " \t")
        ):
            break
        index += 1
    end = index
    while end > start and raw[end - 1] in " \t\r":
        end -= 1
    return start, end


def _unquote_key(raw: str) -> str:
    """去掉键名外层的引号（带空格的键在 YAML 里要加引号）。"""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def _parse_line(raw: str, no: int) -> _LineInfo:
    """把一行原始文本解析成 :class:`_LineInfo`。"""
    offset = len(raw) - len(raw.lstrip())
    body = raw[offset:]
    indent = _indent_of(raw)
    if not body.strip():
        return _LineInfo(no, indent, "blank", "", "", -1, -1, -1)
    if body.startswith("#"):
        return _LineInfo(no, indent, "comment", "", "", -1, -1, -1)
    if body == "-" or body.startswith("- "):
        rest_offset = offset + (2 if body.startswith("- ") else 1)
        rest = raw[rest_offset:]
        colon = _find_colon(rest, rest_offset)
        if colon < 0:
            return _LineInfo(no, indent, "seq", "", "", -1, -1, -1)
        key = _unquote_key(rest[: colon - rest_offset].strip())
        start, end = _scan_value(raw, _skip_space(raw, colon + 1))
        has_value = end > start
        return _LineInfo(
            no,
            indent,
            "seq",
            key,
            raw[start:end] if has_value else "",
            start if has_value else -1,
            end if has_value else -1,
            colon,
        )
    colon = _find_colon(body, offset)
    if colon < 0:
        return _LineInfo(no, indent, "scalar", "", "", -1, -1, -1)
    key = _unquote_key(body[: colon - offset].strip())
    start, end = _scan_value(raw, _skip_space(raw, colon + 1))
    has_value = end > start
    return _LineInfo(
        no,
        indent,
        "map",
        key,
        raw[start:end] if has_value else "",
        start if has_value else -1,
        end if has_value else -1,
        colon,
    )


def _content_lines(infos: List[_LineInfo], start: int, end: int) -> List[int]:
    """返回 ``[start, end)`` 区间内所有「有内容」的行号（跳过空行与注释）。"""
    return [
        i
        for i in range(start, end)
        if infos[i].kind not in ("blank", "comment")
    ]


def _direct_children(
    infos: List[_LineInfo], start: int, end: int
) -> List[int]:
    """返回 ``[start, end)`` 区间内属于**同一层**的行号。

    同一层 = 该区间内内容行的**最小缩进** —— 这就是「缩进栈」的
    最小实现：靠缩进区分父子，而不必维护完整的栈。
    """
    indices = _content_lines(infos, start, end)
    if not indices:
        return []
    base = min(infos[i].indent for i in indices)
    return [i for i in indices if infos[i].indent == base]


def _block_end(
    infos: List[_LineInfo], start: int, indent: int, limit: int
) -> int:
    """返回从 ``start`` 开始的块的结束下标（不含）。

    结束标志是**第一个缩进 <= ``indent`` 的内容行**；空行与注释行
    不算结束标志（它们可能属于块内部）。
    """
    for i in range(start, limit):
        info = infos[i]
        if info.kind in ("blank", "comment"):
            continue
        if info.indent <= indent:
            return i
    return limit


def _value_block_end(
    infos: List[_LineInfo], key_line: int, limit: int
) -> int:
    """返回某个映射键的**值块**结束下标（不含）。

    ⚠️ 为什么要多这一层
    ==================
    YAML 允许块序列与父键**同列**（PyYAML 的默认输出就是这种
    「缩进式序列」）::

        dangerous_commands:
        - 'rm -rf'
        - 'sudo'

    普通 :func:`_block_end` 遇到第一个**同缩进**的序列项就收尾 ——
    于是这个序列被当成「不存在」。写回器接下来会**插入**一份新列表，
    旧列表留在原地：同一个键出现两次，YAML 直接解析失败。

    所以这里要把「与父键同列的序列项」也算进父键的值块。

    :param infos: 全部行信息。
    :param key_line: 映射键所在行号。
    :param limit: 上界。
    :return: 值块的结束下标（不含）。
    """
    info = infos[key_line]
    start = key_line + 1
    end = _block_end(infos, start, info.indent, limit)
    index = end
    last_seq = end
    while index < limit:
        current = infos[index]
        if current.kind in ("blank", "comment"):
            # 空行与注释不改变块的范围 —— 但不能让它挡住后面的序列项。
            index += 1
            continue
        if current.kind == "seq" and current.indent == info.indent:
            # 与父键同列 ⇒ 它就是父键的值（YAML 的缩进式序列）。
            index += 1
            last_seq = index
            continue
        break
    return last_seq


def _subtree_end(infos: List[_LineInfo], line: int, limit: int) -> int:
    """返回该行所属子树（例如一个序列项）的结束下标（不含）。"""
    indent = infos[line].indent
    for i in range(line + 1, limit):
        info = infos[i]
        if info.kind in ("blank", "comment"):
            continue
        if info.indent <= indent:
            return i
    return limit


def _root_scope(infos: List[_LineInfo]) -> _Scope:
    """根层作用域：整份文本里缩进最小的那些行。"""
    children = _direct_children(infos, 0, len(infos))
    return _Scope(
        map_keys=[i for i in children if infos[i].kind == "map"],
        seq_items=[i for i in children if infos[i].kind == "seq"],
    )


def _descend_map(infos: List[_LineInfo], key_line: int) -> _Scope:
    """下钻到某个映射键的块。"""
    indent = infos[key_line].indent
    start = key_line + 1
    end = _block_end(infos, start, indent, len(infos))
    children = _direct_children(infos, start, end)
    return _Scope(
        map_keys=[i for i in children if infos[i].kind == "map"],
        seq_items=[i for i in children if infos[i].kind == "seq"],
    )


def _descend_item(infos: List[_LineInfo], item_line: int) -> _Scope:
    """下钻到某个序列项内部。

    序列项的映射键有两处：项内联映射（写在 ``- key: value`` 那一行）
    与项内缩进的续行。
    """
    info = infos[item_line]
    keys: List[int] = []
    if info.kind == "seq" and info.key:
        keys.append(item_line)
    start = item_line + 1
    end = _block_end(infos, start, info.indent, len(infos))
    keys.extend(
        i
        for i in _direct_children(infos, start, end)
        if infos[i].kind == "map"
    )
    return _Scope(map_keys=keys, seq_items=[])


def _parse_path(path: str) -> Optional[List[Tuple[str, Optional[int]]]]:
    """把 ``a.b[0].c`` 这样的路径解析成分段列表。

    语法不支持时返回 ``None``。
    """
    if not isinstance(path, str) or not path.strip():
        return None
    segments: List[Tuple[str, Optional[int]]] = []
    for raw_part in path.split("."):
        part = raw_part.strip()
        match = _PATH_SEGMENT_RE.match(part)
        if not match:
            return None
        name = match.group(1).strip()
        if not name:
            return None
        index: Optional[int] = None
        if match.group(2) is not None:
            index = int(match.group(2))
        segments.append((name, index))
    return segments


def _locate(
    infos: List[_LineInfo], segments: List[Tuple[str, Optional[int]]]
) -> Optional[int]:
    """按路径分段在文本里下钻，返回目标行号。

    找不到返回 ``None``（**绝不新增**键）。
    """
    scope = _root_scope(infos)
    last = len(segments) - 1
    for pos, (name, index) in enumerate(segments):
        target = None
        for i in scope.map_keys:
            if infos[i].key == name:
                target = i
                break
        if target is None:
            return None
        if index is None:
            if pos == last:
                return target
            scope = _descend_map(infos, target)
            continue
        # ``key[N]``：键的值是一个序列，取第 N 项
        # ⚠️ 用 ``_value_block_end`` —— 它也认得「与父键同列」的缩进式序列。
        start = target + 1
        end = _value_block_end(infos, target, len(infos))
        items = [
            i
            for i in _direct_children(infos, start, end)
            if infos[i].kind == "seq"
        ]
        if index >= len(items):
            return None
        item = items[index]
        if pos == last:
            return item
        scope = _descend_item(infos, item)
    return None


def _quote(text: str) -> str:
    """把字符串编码成双引号 YAML 标量。"""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return '"' + escaped + '"'


def _encode_scalar(value: Any) -> Optional[str]:
    """把标量值编码成 YAML 字面量；不支持的类型返回 ``None``。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _newline_marker(lines: List[str]) -> str:
    """判断文本用的换行风格，返回新行要带的标记（``\\r`` 或空）。"""
    for line in lines:
        if line.endswith("\r"):
            return "\r"
    return ""


def _rewrite_scalar(
    infos: List[_LineInfo],
    lines: List[str],
    target: int,
    encoded: str,
) -> Tuple[bool, str, Optional[List[str]]]:
    """就地替换目标行的标量值（保留缩进、键名写法与行尾注释）。"""
    info = infos[target]
    if info.value_start >= 0:
        raw = lines[target]
        new_raw = raw[: info.value_start] + encoded + raw[info.value_end :]
        return True, "", lines[:target] + [new_raw] + lines[target + 1 :]
    # 该键没有行内值 —— 只有在它下面**没有块**时才能直接写成标量
    start = target + 1
    end = _value_block_end(infos, target, len(infos))
    if _content_lines(infos, start, end):
        return False, "目标键是块结构，无法就地改成标量", None
    raw = lines[target]
    new_raw = raw[: info.colon + 1] + " " + encoded + raw[info.colon + 1 :]
    return True, "", lines[:target] + [new_raw] + lines[target + 1 :]


def _rewrite_list(
    infos: List[_LineInfo],
    lines: List[str],
    target: int,
    value: List[Any],
) -> Tuple[bool, str, Optional[List[str]]]:
    """整块替换目标键下面的序列。"""
    encoded_items: List[str] = []
    for item in value:
        encoded = _encode_scalar(item)
        if encoded is None:
            return (
                False,
                "不支持的值类型：序列元素为 {}".format(type(item).__name__),
                None,
            )
        encoded_items.append(encoded)
    info = infos[target]
    start = target + 1
    # ⚠️ 必须用 ``_value_block_end`` —— 它认得「与父键同列」的缩进式序列。
    #    用普通 ``_block_end`` 会把已有序列当成「不存在」，
    #    于是变成**插入**而不是**替换**，旧序列留在原地 → YAML 解析失败。
    end = _value_block_end(infos, target, len(infos))
    children = _direct_children(infos, start, end)
    seq_items = [i for i in children if infos[i].kind == "seq"]
    marker = _newline_marker(lines)
    if seq_items:
        # 保留原文件的缩进风格（缩进式 / 与父键同列都照原样写回）。
        padding = " " * infos[seq_items[0]].indent
    else:
        padding = " " * (info.indent + 2)
    new_items = [padding + "- " + item + marker for item in encoded_items]
    if seq_items:
        first = seq_items[0]
        last = seq_items[-1]
        del_end = _subtree_end(infos, last, end)
        # 序列块**之后**、且不属于该块的注释/空行保留下来
        while (
            del_end - 1 > last
            and infos[del_end - 1].kind in ("blank", "comment")
            and infos[del_end - 1].indent <= infos[last].indent
        ):
            del_end -= 1
        return True, "", lines[:first] + new_items + lines[del_end:]
    if _content_lines(infos, start, end):
        return False, "目标键是映射块，无法替换为序列", None
    # 没有已有序列：去掉行内值（例如 ``[]``），在原位插入块序列
    raw = lines[target]
    if info.value_start >= 0:
        # 去掉行内值后别留下「冒号后面一个空格」的尾巴。
        raw = raw[: info.value_start].rstrip() + raw[info.value_end :]
    return True, "", lines[:target] + [raw] + new_items + lines[target + 1 :]


def _apply_update(text: str, path: str, value: Any) -> Tuple[bool, str, str]:
    """把单个路径的值改写进文本。

    :return: ``(是否成功, 失败原因, 新文本)``；失败时新文本与传入的相同。
    """
    segments = _parse_path(path)
    if segments is None:
        return False, "路径语法不支持", text
    lines = text.split("\n")
    infos = [_parse_line(raw, i) for i, raw in enumerate(lines)]
    target = _locate(infos, segments)
    if target is None:
        return False, "路径不存在", text
    if isinstance(value, dict):
        return False, "不支持的值类型：映射", text
    if isinstance(value, list):
        ok, reason, new_lines = _rewrite_list(infos, lines, target, value)
    else:
        encoded = _encode_scalar(value)
        if encoded is None:
            return (
                False,
                "不支持的值类型：{}".format(type(value).__name__),
                text,
            )
        ok, reason, new_lines = _rewrite_scalar(infos, lines, target, encoded)
    if not ok or new_lines is None:
        return False, reason, text
    return True, "", "\n".join(new_lines)


def update_config_text(
    text: str, updates: Sequence[Tuple[str, Any]]
) -> Tuple[str, List[str], List[str]]:
    """在配置原文里**原地**改写若干个键的值。

    只替换目标键的**值**，其余字节（注释、空行、缩进风格、换行风格）
    原样保留 —— 绝不用 ``yaml.safe_dump()`` 整体重写，那会把用户写的
    中文注释全部抹掉。

    支持的路径语法（极简的块结构解析，不依赖 ruamel）：

    * ``llm.model`` —— 嵌套映射；
    * ``rules.hot_reload.enabled`` —— 多层嵌套；
    * ``rules.dangerous_commands`` —— 值是块序列，整块替换；
    * ``alert.channels[0].url`` —— 序列项里的字段。

    找不到的路径**一律跳过**，绝不新增键；单条失败不影响其它路径。

    :param text: 配置原文。
    :param updates: ``(yaml 路径, 新值)`` 序列；同一路径出现多次时
        只应用最后一次。
    :return: ``(新文本, 成功应用的路径, 跳过的路径)``。
    """
    if not updates:
        return text, [], []
    # 同一路径只保留最后一次的值
    wanted: Dict[str, Any] = dict(updates)
    applied: List[str] = []
    skipped: List[str] = []
    current = text
    for path, value in wanted.items():
        try:
            ok, reason, current = _apply_update(current, path, value)
        except Exception as exc:  # noqa: BLE001 —— 单条写回失败不能拖垮整次保存
            # 带上异常类型：``str(exc)`` 常常是空串，光看它定位不到问题。
            ok, reason = False, "写回失败：{}: {}".format(
                type(exc).__name__, exc
            )
        if ok:
            applied.append(path)
        else:
            skipped.append(path)
            logger.warning("配置写回跳过 %s：%s", path, reason)
    return current, applied, skipped


def _render_key(
    key: str,
    value: Any,
    indent: int,
    marker: str,
) -> Tuple[List[str], List[str]]:
    """把一个键渲染成 YAML 块文本。

    :return: ``(行列表, 路径列表)``；路径含该键本身及其后代，
        相对父级映射。值类型不支持时返回空行列表。
    """
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + key + ": {}" + marker], [key]
        lines = [pad + key + ":" + marker]
        paths = [key]
        for sub_key, sub_value in value.items():
            sub_lines, sub_paths = _render_key(
                sub_key, sub_value, indent + 2, marker
            )
            if not sub_lines:
                continue
            lines.extend(sub_lines)
            paths.extend(key + "." + sub_path for sub_path in sub_paths)
        return lines, paths
    if isinstance(value, list):
        if not value:
            return [pad + key + ": []" + marker], [key]
        lines = [pad + key + ":" + marker]
        for item in value:
            encoded = _encode_scalar(item)
            if encoded is None:
                return [], []
            lines.append(pad + "  - " + encoded + marker)
        return lines, [key]
    encoded = _encode_scalar(value)
    if encoded is None:
        return [], []
    return [pad + key + ": " + encoded + marker], [key]


def _ensure_one(
    lines: List[str],
    parent_line: Optional[int],
    indent: int,
    key: str,
    value: Any,
    marker: str,
) -> Tuple[List[str], List[str], List[str]]:
    """确保某一层映射里存在 ``key``；**已存在则保留用户原值**。

    :param parent_line: 父级映射键所在行号；``None`` 表示根层。
    :return: ``(新行列表, 补上的路径, 跳过的路径)``；路径相对父级映射。
    """
    infos = [_parse_line(raw, i) for i, raw in enumerate(lines)]
    if parent_line is None:
        scope = _root_scope(infos)
        insert_at = len(lines)
    else:
        parent_indent = infos[parent_line].indent
        scope = _descend_map(infos, parent_line)
        insert_at = _block_end(
            infos, parent_line + 1, parent_indent, len(infos)
        )
    # 插入点往前回退掉尾部空行：新键要落在块里，而不是块后面的空行之后。
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1
    target = None
    for i in scope.map_keys:
        if infos[i].key == key:
            target = i
            break
    if target is not None:
        if not isinstance(value, dict):
            # 已有键：一律保留用户的值，绝不覆盖。
            return lines, [], []
        info = infos[target]
        start = target + 1
        end = _block_end(infos, start, info.indent, len(infos))
        children = _direct_children(infos, start, end)
        if not children:
            # 既有键是个标量，却需要映射 —— 类型冲突，跳过。
            return lines, [], [key]
        child_indent = infos[children[0]].indent
        child_lines, child_added, child_skipped = _ensure_level(
            lines, target, child_indent, value, marker
        )
        return (
            child_lines,
            [key + "." + path for path in child_added],
            [key + "." + path for path in child_skipped],
        )
    block, paths = _render_key(key, value, indent, marker)
    if not block:
        return lines, [], [key]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    return new_lines, paths, []


def _ensure_level(
    lines: List[str],
    parent_line: Optional[int],
    indent: int,
    mapping: Dict[str, Any],
    marker: str,
) -> Tuple[List[str], List[str], List[str]]:
    """确保某一层映射里存在 ``mapping`` 的全部键。

    :return: ``(新行列表, 补上的路径, 跳过的路径)``；路径相对本层映射。
    """
    added: List[str] = []
    skipped: List[str] = []
    for key, value in mapping.items():
        new_lines, sub_added, sub_skipped = _ensure_one(
            lines, parent_line, indent, key, value, marker
        )
        lines = new_lines
        added.extend(sub_added)
        skipped.extend(sub_skipped)
    return lines, added, skipped


def ensure_config_defaults(
    text: str, defaults: Dict[str, Any]
) -> Tuple[str, List[str], List[str]]:
    """为**缺失**的配置键补上默认值（只补缺失，绝不改已有值）。

    :func:`update_config_text` 找不到路径时会跳过，因此**新增**的配置键
    （例如 v3.7.0 引入的开关）在用户的旧配置里永远写不回去。本函数
    补上这一段：只往里**加**缺失的键，已有键一律原样保留 ——
    因此天然幂等，重复调用不会改变任何东西。

    :param text: 配置原文。
    :param defaults: 嵌套字典，形如
        ``{"rules": {"l1_action": "red_line_only"}}``。
    :return: ``(新文本, 补上的路径, 跳过的路径)``。
    """
    if not defaults:
        return text, [], []
    lines = text.split("\n")
    marker = _newline_marker(lines)
    new_lines, added, skipped = _ensure_level(lines, None, 0, defaults, marker)
    result = "\n".join(new_lines)
    if added and result and not result.endswith("\n"):
        result += "\n"
    for path in added:
        logger.info("配置补齐默认键：%s", path)
    for path in skipped:
        logger.warning("配置补齐跳过 %s：类型冲突或值类型不支持", path)
    return result, added, skipped


def _read_text(path: str) -> Tuple[Optional[str], bool]:
    """读取配置原文，返回 ``(文本, 是否带 BOM)``。

    读取失败返回 ``(None, False)``；BOM 会被剥掉（写回时再加回去）。
    """
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None, False
    if text.startswith(_BOM):
        return text[len(_BOM) :], True
    return text, False


def read_config_text(path: str) -> Optional[str]:
    """读取配置文件的原文（不做任何解析、不改换行风格）。

    读取失败返回 ``None``。
    """
    text, _ = _read_text(path)
    return text


def save_config(
    config_path: str,
    updates: Sequence[Tuple[str, Any]],
    *,
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把改动写回配置文件（先备份，再原地覆盖）。

    写回前会把原文另存为 ``<配置路径>.bak``；一个字节都没改成的时候
    既不写文件也不产生备份。

    :param config_path: 配置文件路径。
    :param updates: ``(yaml 路径, 新值)`` 序列。
    :param defaults: 需要**补齐**的缺失键（嵌套字典）。写回器找不到
        路径就跳过，所以**新增**的配置键必须先补齐才写得回去。
    :return: 结果字典；失败时 ``ok`` 为 ``False`` 并带 ``error``，
        成功时带 ``applied`` / ``skipped`` / ``backup`` / ``changed``。
    """
    if not config_path:
        return {"ok": False, "error": "配置文件路径为空"}
    directory = os.path.dirname(os.path.abspath(config_path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        # 目录建不出来**不致命** —— 下面会检查文件在不在。
        # 但要留痕：否则「配置写不进去」会变成一句没头没尾的报错。
        logger.warning(
            "创建配置目录失败（%s）：%s: %s", directory, type(exc).__name__, exc
        )
    if not os.path.isfile(config_path):
        return {
            "ok": False,
            "error": "配置文件不存在：{}".format(config_path),
        }
    original, has_bom = _read_text(config_path)
    if original is None:
        return {
            "ok": False,
            "error": "配置文件读取失败：{}".format(config_path),
        }
    text = original
    added_defaults: List[str] = []
    if defaults:
        try:
            text, added_defaults, _ = ensure_config_defaults(
                original, defaults
            )
        except Exception as exc:  # noqa: BLE001 —— 补齐失败不该拖垮保存
            logger.warning(
                "补齐配置默认值失败：%s: %s", type(exc).__name__, exc
            )
            text = original
            added_defaults = []
    new_text, applied, skipped = update_config_text(text, updates)
    if new_text == original:
        return {
            "ok": True,
            "path": config_path,
            "applied": applied,
            "skipped": skipped,
            "added_defaults": added_defaults,
            "backup": "",
            "changed": False,
        }
    backup_path = config_path + ".bak"
    try:
        with open(backup_path, "w", encoding="utf-8", newline="") as handle:
            handle.write((_BOM if has_bom else "") + original)
    except OSError as exc:
        return {
            "ok": False,
            "error": "备份失败：{}".format(exc),
            "path": config_path,
        }
    try:
        with open(config_path, "w", encoding="utf-8", newline="") as handle:
            handle.write((_BOM if has_bom else "") + new_text)
    except OSError as exc:
        return {
            "ok": False,
            "error": "写入失败：{}".format(exc),
            "path": config_path,
        }
    return {
        "ok": True,
        "path": config_path,
        "applied": applied,
        "skipped": skipped,
        "added_defaults": added_defaults,
        "backup": backup_path,
        "changed": True,
    }
