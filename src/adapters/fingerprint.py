"""线程指纹 —— 让多路 Agent 的日志**互不串台**（v3.5.0）。

问题
====

多个 Agent 并行时，它们可能往**同一个**日志文件里写。
于是哨兵读到的是一条**交织**的流：

.. code-block:: text

    {"type":"tool_call","thread_id":"A","tool_call":{"name":"Bash","arguments":{"command":"ls"}}}
    {"type":"tool_call","thread_id":"B","tool_call":{"name":"Bash","arguments":{"command":"rm -rf /"}}}
    {"type":"tool_call","thread_id":"A","tool_call":{"name":"Read","arguments":{"file_path":"a.py"}}}

如果不加区分，这三行进的是**同一个**滑动窗口 ——
于是「A 读了 a.py」和「B 删库」被当成**同一个 Agent 的连续行为**。
漂移检测于是把 A 的正常动作判成「突然要删库」，用户收到一条莫名其妙的告警。

这就是「串台」。

怎么解决
========

给每一行事件打一个**指纹**，指纹相同的才是**同一路**。
指纹的来源按优先级排：

1. **线程 ID**（``thread_id`` / ``threadId`` / ``thread`` / ``conversation_id``）——
   最可靠，那是 Agent 自己说的「我是哪一路」；
2. **会话 ID**（``session_id`` / ``sessionId`` / ``session``）；
3. **工作目录**（``cwd`` / ``working_directory`` / ``project_dir`` …）——
   两个任务在**不同项目目录**里跑时，这就是天然的区分。

⚠️ 有一条**刻意的克制**
-----------------------

**认不出就不认。**

指纹提取不出来时返回**空串**，调用方据此把所有认不出的事件
归到「默认那一路」—— 和 v3.4.0 之前的行为**完全一样**。

为什么不拿「工具参数里的文件路径」当指纹？
因为那会把**同一个 Agent** 拆成一堆碎片
（读 ``D:\\项目\\my-app\\src\\a.py`` 得出 ``…\\src``，
读 ``…\\tests\\b.py`` 得出 ``…\\tests`` ——
同一条线程被切成两个「路」，隔离反而把上下文打碎了）。

**宁可不合，也不要错拆。**

同理，指纹里**不含时间戳、序号、UUID 之外的东西** ——
同一个线程的每一行必须得出**同一个**指纹。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

__all__ = [
    "FINGERPRINT_PATH_PREFIX",
    "FINGERPRINT_SESSION_PREFIX",
    "FINGERPRINT_THREAD_PREFIX",
    "extract_fingerprint",
    "fingerprint_label",
    "normalize_path",
]

#: 线程 ID 前缀。
FINGERPRINT_THREAD_PREFIX = "thread:"

#: 会话 ID 前缀。
FINGERPRINT_SESSION_PREFIX = "session:"

#: 工作目录前缀。
FINGERPRINT_PATH_PREFIX = "path:"

#: 载荷里可能承载**线程 ID** 的键（按优先级）。
_THREAD_KEYS = (
    "thread_id",
    "threadId",
    "thread",
    "conversation_id",
    "conversationId",
    "conversation",
)

#: 载荷里可能承载**会话 ID** 的键（按优先级）。
_SESSION_KEYS = (
    "session_id",
    "sessionId",
    "session",
)

#: 载荷里可能承载**工作目录**的键（按优先级）。
_CWD_KEYS = (
    "cwd",
    "working_directory",
    "workingDirectory",
    "workdir",
    "project_dir",
    "projectDir",
    "root_path",
    "rootPath",
)

#: 嵌套结构的**最大探测深度**。
#:
#: ⚠️ 不能无限递归 —— 那会为了找一个键把整个载荷翻一遍。
#: 两层足够覆盖「顶层 → item → thread」这种常见嵌套。
_MAX_DEPTH = 2

#: 路径里要**去掉**的噪声片段（统一成斜杠后处理）。
_WINDOWS_DRIVE_RE = re.compile(r"^[a-z]:/", re.IGNORECASE)


def normalize_path(path: Any) -> str:
    """把路径归一化成「可比较」的形态。

    做三件事：

    1. 反斜杠换成正斜杠（Windows 上 ``D:\\项目`` 与 ``D:/项目`` 是同一个地方）；
    2. 去掉结尾的斜杠（``D:/项目/`` 与 ``D:/项目`` 是同一个地方）；
    3. 转小写（Windows 上路径**不区分大小写**）。

    :param path: 原始路径。
    :return: 归一化后的路径；认不出来就是空串。
    """
    text = str(path or "").strip().strip('"').strip("'")
    if not text:
        return ""
    text = text.replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    text = _WINDOWS_DRIVE_RE.sub("", text)
    return text.lower()


def _first_str(payload: Any, keys) -> str:
    """从字典里按优先级取第一个非空字符串。

    :param payload: 字典。
    :param keys: 候选键名（按优先级）。
    :return: 取到的字符串；没有就是空串。
    """
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        # 有些 CLI 把 ID 写成数字（codex 的 thread_id 就是数字）。
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _find_nested(payload: Any, keys, depth: int = 0) -> str:
    """在（有限深度的）嵌套结构里找第一个非空值。

    :param payload: 载荷。
    :param keys:    候选键名。
    :param depth:   当前深度。
    :return: 找到的值；没有就是空串。
    """
    found = _first_str(payload, keys)
    if found:
        return found
    if depth >= _MAX_DEPTH or not isinstance(payload, dict):
        return ""
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_nested(value, keys, depth + 1)
            if found:
                return found
    return ""


def extract_fingerprint(payload: Dict[str, Any]) -> str:
    """从一行事件的载荷里提取**线程指纹**。

    :param payload: 适配器解析出来的原始载荷。
    :return: 指纹字符串；认不出来就是**空串**（= 归到默认那一路）。
    """
    if not isinstance(payload, dict):
        return ""

    thread = _find_nested(payload, _THREAD_KEYS)
    if thread:
        return FINGERPRINT_THREAD_PREFIX + thread

    session = _find_nested(payload, _SESSION_KEYS)
    if session:
        return FINGERPRINT_SESSION_PREFIX + session

    cwd = _find_nested(payload, _CWD_KEYS)
    if cwd:
        normalized = normalize_path(cwd)
        if normalized:
            return FINGERPRINT_PATH_PREFIX + normalized
    return ""


def fingerprint_label(fingerprint: str) -> str:
    """把指纹渲染成一句**人能看懂**的短标签。

    给看板的虚拟会话卡片用 —— 用户看到的是「工作区 my-app」，
    而不是 ``path:d:/项目/my-app``。

    :param fingerprint: 指纹字符串。
    :return: 标签文本；认不出来就是「默认流」。
    """
    text = str(fingerprint or "").strip()
    if not text:
        return "默认流"
    if text.startswith(FINGERPRINT_THREAD_PREFIX):
        return "线程 " + text[len(FINGERPRINT_THREAD_PREFIX):][:12]
    if text.startswith(FINGERPRINT_SESSION_PREFIX):
        return "会话 " + text[len(FINGERPRINT_SESSION_PREFIX):][:12]
    if text.startswith(FINGERPRINT_PATH_PREFIX):
        tail = text[len(FINGERPRINT_PATH_PREFIX):].rstrip("/")
        parts = [part for part in tail.split("/") if part]
        if not parts:
            return "默认流"
        return "工作区 " + parts[-1][:24]
    return text[:24]


def fingerprint_stream_key(fingerprint: str) -> str:
    """把指纹变成**流标识**（看板与日志行里用的那个 key）。

    空指纹归到 ``default`` —— 所有认不出的事件共用一路。

    :param fingerprint: 指纹字符串。
    :return: 流标识。
    """
    text = str(fingerprint or "").strip()
    return text or "default"


def fingerprint_of_event(event: Optional[Dict[str, Any]]) -> str:
    """从**统一事件**里取指纹（适配器已经打好标的那种）。

    :param event: 统一事件字典。
    :return: 指纹字符串；没有就是空串。
    """
    if not isinstance(event, dict):
        return ""
    return str(event.get("fingerprint") or "")
