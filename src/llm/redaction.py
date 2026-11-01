"""L2 输入脱敏 —— 送模型前与落盘前都必须过一遍。

为什么必须做
============
哨兵送进本地模型的，是**被监控 Agent 的日志原文**。原文里完全可能出现：

* API Key / Token / 密码（Agent 正在读配置、正在调试鉴权）；
* 带密码的数据库连接串；
* 用户隐私数据。

这些内容一旦进了**告警日志**，就等于把密钥复制到了另一个文件里 ——
而且 `logs/sentinel_alert.log` 往往比原日志更容易被人随手打开、随手转发。

所以脱敏必须发生在**两个位置**：

1. **送模型前** —— 不让模型看到密钥（模型可能把它复述进解释里）；
2. **落盘前** —— 不让密钥进告警日志。

脱敏规则提取自旧项目 ``core/stream_parser.py:129-131`` 的
``redact_secrets``（后端强制脱敏，不依赖前端）。

设计原则
========
* **只替换、不删除** —— 保留上下文可读性，让人知道「这里本来有个密钥」；
* **宁可多脱一点** —— 误脱一个环境变量名，只是告警里少几个字；
* **纯函数、无 IO** —— 便于单测。
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

__all__ = ["REDACTION_MARKER", "redact_secrets", "redaction_patterns"]

#: 替换标记。刻意用一对尖括号，便于在告警里一眼认出来。
REDACTION_MARKER = "«REDACTED»"


def _rules() -> List[Tuple[str, Pattern[str]]]:
    """构造脱敏规则表。

    每条规则是 ``(类别名, 正则)``；正则的**第一个捕获组**是要被替换的片段。
    """
    return [
        # ---- 私钥块 ----
        (
            "PRIVATE_KEY",
            re.compile(
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
                r"[\s\S]*?"
                r"-----END [A-Z ]*PRIVATE KEY-----"
            ),
        ),
        # ---- 显式赋值型（最常见）----
        # api_key=xxx / "password": "xxx" / token: xxx
        (
            "ASSIGNED_SECRET",
            re.compile(
                r"(?i)\b(api[_-]?key|apikey|secret|secret[_-]?key|token|access[_-]?token"
                r"|refresh[_-]?token|password|passwd|pwd|passphrase|credential"
                r"|private[_-]?key|auth)\b"
                r"(\s*[:=]\s*)"
                r"(\"[^\"\n]{6,}\"|'[^'\n]{6,}'|[A-Za-z0-9_\-./+]{8,})"
            ),
        ),
        # ---- 各家已知格式的密钥 ----
        ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
        ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
        ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
        ("GITHUB_PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
        ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
        ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
        ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
        ("STRIPE_KEY", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
        # ---- JWT ----
        ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
        # ---- Authorization 头 ----
        (
            "BEARER",
            re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9\-._~+/]{12,}=*)"),
        ),
        # ---- 带密码的连接串 ----
        (
            "CONNECTION_STRING",
            re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@]{3,})(@)"),
        ),
    ]


def redaction_patterns() -> List[Tuple[str, Pattern[str]]]:
    """返回脱敏规则表（供测试与文档使用）。"""
    return _rules()


def redact_secrets(text: str) -> str:
    """把文本里的疑似密钥替换成脱敏标记。

    :param text: 原始文本。
    :return: 脱敏后的文本；``None`` 或空串原样返回。
    """
    if not text or not isinstance(text, str):
        return text

    result = text

    # --- 私钥块：整块替换 ---
    result = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        REDACTION_MARKER,
        result,
    )

    for name, pattern in _rules():
        if name == "PRIVATE_KEY":
            continue
        if name == "ASSIGNED_SECRET":
            # 只替换值，保留键名 —— 键名本身是判断「这里有敏感信息」的线索
            result = pattern.sub(
                lambda m: f"{m.group(1)}{m.group(2)}{REDACTION_MARKER}",
                result,
            )
        elif name == "BEARER":
            result = pattern.sub(
                lambda m: f"{m.group(1)} {REDACTION_MARKER}",
                result,
            )
        elif name == "CONNECTION_STRING":
            result = pattern.sub(
                lambda m: f"{m.group(1)}{REDACTION_MARKER}{m.group(3)}",
                result,
            )
        else:
            result = pattern.sub(REDACTION_MARKER, result)

    return result
