"""Agent CLI 的**鉴权失败**与**运行期失败**识别。

为什么需要单独一层
==================

这些 CLI 未登录时**退出码往往是 0**，只往 stderr 打一行
「Authentication required. Please use /login command to sign in」。
于是「进程成功退出」被误判成「任务完成」，那行登录提示被当成
「任务输出」原样交给上层模型 —— 模型只能从一段莫名其妙的输出里
猜「哦原来要登录」。

更麻烦的是**退出码不能当门槛**：gemini 未登录是 41、command-code 是 3、
claude 却是 0。所以只能靠**措辞**认。

两层结构
========

1. :data:`GLOBAL_AUTH_PATTERNS` —— 全局兜底表，按最初 9 个 CLI 的
   真实措辞写的；
2. 每条 Agent 档案里的 ``auth_patterns`` —— 新产品自带的专属措辞，
   扩展它**不需要改本模块一行代码**。

三条门槛
========

1. stderr 必须**短**（≤ :data:`AUTH_MAX_CHARS` 字符）——
   真跑完任务的 Agent 一定会把结果写进 stdout，stderr 只可能是短的；
2. 只看 stderr **开头** :data:`AUTH_HEAD_CHARS` 个字符 ——
   真跑完任务的 Agent 结果在 stdout 正文里，不会把登录措辞放最前面；
3. stdout 里**没有实质内容** —— stdout 全是噪声行（警告 / 空行）时仍判，
   一旦出现非噪声的正文就不再判，这样「让人写登录页」也不会被误伤。

来源
====

移植自开源项目 ``agent_bridge`` 的 ``bridge/runner.py``
（``_AUTH_PATTERNS`` / ``_match_head`` / ``_has_substantive_output``）。
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, List, Pattern, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_HEAD_CHARS",
    "AUTH_MAX_CHARS",
    "GLOBAL_AUTH_PATTERNS",
    "compile_patterns",
    "detect_auth_failure",
    "detect_runtime_failure",
    "has_substantive_output",
    "strip_ansi",
]

#: ANSI 转义序列（CLI 输出里常见，要先剥掉再判措辞）。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: 噪声行前缀 —— 这些行不算「实质输出」。
_NOISE_PREFIXES: Tuple[str, ...] = (
    "(node:",
    "node:",
    "npm ",
    "warning:",
    "experimentalwarning",
)

#: 鉴权提示的搜索范围：只看输出开头这么多字符。
AUTH_HEAD_CHARS = 400

#: 只有「短输出」才可能是登录提示。
AUTH_MAX_CHARS = 800

#: 全局兜底的鉴权措辞表。
GLOBAL_AUTH_PATTERNS: Tuple[Pattern[str], ...] = (
    re.compile(r"authentication\s+required", re.IGNORECASE),
    re.compile(r"unauthenticated", re.IGNORECASE),
    re.compile(
        r"not\s+(?:logged\s?in|authenticated|signed\s?in)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:please\s+)?use\s+`?/login`?", re.IGNORECASE),
    re.compile(r"(?:please\s+)?run\s+`?/login`?", re.IGNORECASE),
    re.compile(r"invalid\s+api\s*key", re.IGNORECASE),
    re.compile(
        r"credentials?\s+(?:expired|invalid|not\s+found|missing)",
        re.IGNORECASE,
    ),
    re.compile(r"请(?:先)?登录|尚未登录|未登录|需要登录|登录后(?:再|才)"),
)


def strip_ansi(text: str) -> str:
    """剥掉 ANSI 转义序列。

    :param text: 原始文本。
    :return: 剥干净的文本。
    """
    return _ANSI_RE.sub("", text or "")


def compile_patterns(
    raw_patterns: Iterable[str],
) -> Tuple[Pattern[str], ...]:
    """把字符串正则编译成正则对象。

    无效的正则**只记一条 warning 并跳过** —— 一个写错的
    ``auth_patterns`` 不该让整个派发失败。

    :param raw_patterns: 正则源串。
    :return: 编译好的正则元组。
    """
    compiled: List[Pattern[str]] = []
    for raw in raw_patterns or ():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            compiled.append(re.compile(text, re.IGNORECASE))
        except re.error:
            logger.warning("特征表里的正则无效，已忽略：%r", text)
    return tuple(compiled)


def has_substantive_output(stdout: str) -> bool:
    """stdout 里是否有「像任务结果」的内容。

    滤掉警告、空行、纯符号行；剩余任一行长度 ≥ 8 即视为有正文。

    :param stdout: 进程标准输出。
    :return: 有实质内容时返回 ``True``。
    """
    for line in strip_ansi(stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(lowered.startswith(prefix) for prefix in _NOISE_PREFIXES):
            continue
        if "warning" in lowered or lowered.startswith("at "):
            continue
        if len(stripped) >= 8:
            return True
    return False


def _match_head(
    stdout: str,
    stderr: str,
    patterns: Sequence[Pattern[str]],
) -> str:
    """在 stderr 开头找第一个命中的特征。

    :param stdout: 进程标准输出。
    :param stderr: 进程标准错误。
    :param patterns: 待匹配的正则。
    :return: 命中那一行；没命中返回空串。
    """
    text = (stderr or "").strip()
    if not text or len(text) > AUTH_MAX_CHARS:
        return ""
    head = text[:AUTH_HEAD_CHARS]
    matched = next((p for p in patterns if p.search(head)), None)
    if matched is None or has_substantive_output(stdout):
        return ""
    for line in text.splitlines():
        if line.strip() and matched.search(line):
            return line.strip()
    fallback = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        head[:120],
    )
    return fallback


def detect_auth_failure(
    stdout: str,
    stderr: str,
    extra_patterns: Iterable[str] = (),
) -> str:
    """识别「CLI 因为没登录而拒绝干活」。

    :param stdout: 进程标准输出。
    :param stderr: 进程标准错误。
    :param extra_patterns: 该 Agent 专属的鉴权措辞。
    :return: 提示首行；不是这种情况时返回空串。
    """
    patterns = GLOBAL_AUTH_PATTERNS + compile_patterns(extra_patterns)
    return _match_head(stdout, stderr, patterns)


def detect_runtime_failure(
    stdout: str,
    stderr: str,
    fail_patterns: Iterable[str] = (),
) -> str:
    """识别「CLI 起来了但依赖没就绪」。

    与鉴权失败**分开**，因为处置动作不同：鉴权失败要「去登录」，
    运行期失败要「去起服务」（比如 hermes 的 gateway 没跑）。

    判定门槛与鉴权失败一致（stderr 短、只看开头、stdout 无正文），
    但**不做**全局兜底 —— 运行期失败没有通用措辞。

    :param stdout: 进程标准输出。
    :param stderr: 进程标准错误。
    :param fail_patterns: 该 Agent 专属的失败措辞。
    :return: 提示首行；不是这种情况时返回空串。
    """
    patterns = compile_patterns(fail_patterns)
    if not patterns:
        return ""
    return _match_head(stdout, stderr, patterns)
