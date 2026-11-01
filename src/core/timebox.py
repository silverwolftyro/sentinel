"""时间盒（Time-Box）检测器 —— 把《Agent 跑偏警示录》变成产品逻辑（v3.3.0）。

为什么要有这个
==============

《Agent 跑偏警示录》里最深刻的一条根因是：

    「一个子任务能吃掉整轮」——
    下载依赖、修测试、给即将删掉的引擎装权重，
    一件事干到天荒地老，主任务却一动没动。

而 v3.2.1 之前的哨兵**只认「行为特征」**：
删文件（红线）、读同一个文件（死循环）、偏离目标（意图漂移）。
「**一直在同一件事上磨**」这件事，**一次都没被检测过** ——
它既不删东西，又不偏离目标，看起来「很安全」。

所以这一版加一条**正交**的判定维度：**时间盒**。

判定逻辑
========

哨兵给「同一类操作」开一个**时间盒**（默认 15 分钟）。
只要 Agent 在这个盒子里**反复做同一件事**，盒子的时间用完 → 判定跑偏。

两种桶（bucket）
----------------

1. **同一条命令重复**（``cmd:`` 桶）——
   同一条命令（归一化后）重复 ≥ ``min_repeats`` 次，
   且首次出现到现在 ≥ ``window_minutes`` → 判定跑偏。
   *例：同一个 pytest 用例反复重跑、pip install 反复重试。*
2. **同一类操作堆积**（``cat:`` 桶）——
   同一类操作（装依赖 / 跑测试 / 编译）累计 ≥ ``min_repeats`` 次，
   且首次出现到现在 ≥ ``window_minutes`` → 判定跑偏。
   *例：换着花样跑不同的测试，但全是测试。*

⚠️ 只有**用户明确列出来**的类别才启用「同类堆积」判定
（配置项 ``categories``，默认 ``["dependency", "test"]``）——
「一直在改代码」是**正常**的，不该被判成跑偏。

⚠️ 判定**只看事件流**，看不到「一条命令跑了 30 分钟」这种长耗时操作
（哨兵只读日志，不读进程的运行时间）。
这类情况靠「同一条命令重复出现」兜底 —— 长命令通常会被重试。

 Guardian 联动
=============

时间盒**跟着守护者的武装状态走**（``rules.timebox.guardian_suspend``
默认**留空**）：

* 守护者武装着（``enabled`` 且 ``intervention_mode=guardian``）
  → 超时死磕会被**自动挂起**；
* 守护者没武装 → **只告警**，不碰别人的进程；
* 显式写 ``false`` → 永远只告警；
* 显式写 ``true`` → 只要**有**守护者就挂。

为什么默认跟着守护者走
======================

守护者本身就是「用户授权我去动手」的开关 ——
用户把 ``intervention_mode`` 设成 ``guardian``，
就等于说了「该拦的给我拦」。
「超时死磕」也是一种该拦的情况，
所以不必再要用户配一遍。

挂起是**可逆**的（用户在看板上点一下就恢复）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "RULE_ID_TIMEBOX",
    "TIMEBOX_ALERT_REASON",
    "VERDICT_TIMEBOX",
    "TimeBoxDetector",
    "classify_operation",
]

#: 时间盒判定名。
VERDICT_TIMEBOX = "TIMEBOX"

#: 时间盒规则 ID。
RULE_ID_TIMEBOX = "timebox_exceeded"

#: 时间盒告警的固定开头（用户可见的承诺）。
TIMEBOX_ALERT_REASON = "[CRITICAL] 严重跑偏"

#: 默认时间盒长度（分钟）。
DEFAULT_WINDOW_MINUTES = 15.0

#: 默认最少重复次数。
DEFAULT_MIN_REPEATS = 3

#: 默认盯着的类别（只有这几类会启用「同类堆积」判定）。
DEFAULT_CATEGORIES = ("dependency", "test")

#: 类别 → 中文名。
CATEGORY_LABELS = {
    "dependency": "装依赖 / 下载",
    "test": "跑测试",
    "build": "编译打包",
    "edit": "改代码",
    "search": "搜索",
    "other": "其它操作",
}

#: 「装依赖 / 下载」的命令特征。
_DEPENDENCY_PATTERNS = (
    r"\bpip3?\s+(?:install|download)\b",
    r"\buv\s+(?:pip\s+)?(?:install|add|sync)\b",
    r"\bpoetry\s+(?:install|add|lock|update)\b",
    r"\bpdm\s+(?:install|add|update)\b",
    r"\bnpm\s+(?:i|install|ci|add)\b",
    r"\b(?:pnpm|yarn)\s+(?:install|add|update)\b",
    r"\bcargo\s+(?:fetch|install|add|update)\b",
    r"\bgo\s+(?:mod\s+download|get)\b",
    r"\bconda\s+(?:install|env\s+update)\b",
    r"\bapt(?:-get)?\s+install\b",
    r"\bbrew\s+install\b",
    r"\bwinget\s+install\b",
    r"\bchoco\s+install\b",
    r"\bnuget\s+(?:install|restore)\b",
    r"\bdotnet\s+restore\b",
    r"\bgradle\s+dependencies\b",
    r"\bmvn\s+(?:install|dependency:.*)\b",
    r"\bcurl\b|\bwget\b",
    r"\bgit\s+clone\b",
    r"\bhuggingface-cli\s+download\b",
    r"\bhf\s+download\b",
)

#: 「跑测试」的命令特征。
_TEST_PATTERNS = (
    r"\bpytest\b",
    r"\bunittest\b",
    r"\bnose2?\b",
    r"\btox\b",
    r"\bnpm\s+(?:run\s+)?test\b",
    r"\byarn\s+test\b",
    r"\bpnpm\s+(?:run\s+)?test\b",
    r"\bjest\b",
    r"\bmocha\b",
    r"\bvitest\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+test\b",
    r"\bmvn\s+test\b",
    r"\bgradle\s+test\b",
    r"\bdotnet\s+test\b",
    r"\bphpunit\b",
    r"\brspec\b",
    r"\bctest\b",
)

#: 「编译打包」的命令特征。
_BUILD_PATTERNS = (
    r"\bmake\b",
    r"\bcmake\b",
    r"\bninja\b",
    r"\btsc\b",
    r"\bwebpack\b",
    r"\bvite\s+build\b",
    r"\bnpm\s+(?:run\s+)?build\b",
    r"\byarn\s+build\b",
    r"\bpnpm\s+(?:run\s+)?build\b",
    r"\bcargo\s+build\b",
    r"\bgo\s+build\b",
    r"\bgradle\s+(?:build|assemble)\b",
    r"\bmvn\s+(?:package|compile)\b",
    r"\bdotnet\s+build\b",
    r"\bmsbuild\b",
    r"\bpyinstaller\b",
    r"\bdocker\s+build\b",
)

#: 「搜索」的命令特征。
_SEARCH_PATTERNS = (
    r"\b(?:rg|ripgrep|grep|ag|ack)\b",
    r"\bfind\b",
    r"\bgit\s+(?:log|grep|blame)\b",
)

#: 归一化后**不计入**重复统计的「廉价查看」命令。
#:
#: 这些命令重复几次都很正常（Agent 反复 ``git status`` 看看改没改），
#: 不该被判成「死磕」。
_IGNORED_COMMANDS = (
    "git status",
    "git diff",
    "git log",
    "git branch",
    "ls",
    "dir",
    "pwd",
)

_WHITESPACE = re.compile(r"\s+")
_DEPENDENCY_RE = re.compile("|".join(_DEPENDENCY_PATTERNS), re.IGNORECASE)
_TEST_RE = re.compile("|".join(_TEST_PATTERNS), re.IGNORECASE)
_BUILD_RE = re.compile("|".join(_BUILD_PATTERNS), re.IGNORECASE)
_SEARCH_RE = re.compile("|".join(_SEARCH_PATTERNS), re.IGNORECASE)


def classify_operation(command: str, tool_name: str = "") -> str:
    """把一次操作归到**类别**里。

    :param command: 工具命令。
    :param tool_name: 工具名。
    :return: 类别名（``dependency`` / ``test`` / ``build`` / ``edit`` /
        ``search`` / ``other``）。
    """
    text = str(command or "").strip()
    if not text:
        name = str(tool_name or "").lower()
        if name in ("edit", "write", "multiedit", "str_replace_editor"):
            return "edit"
        if name in ("read", "glob", "grep", "search", "codebase_search"):
            return "search"
        return "other"
    if _DEPENDENCY_RE.search(text):
        return "dependency"
    if _TEST_RE.search(text):
        return "test"
    if _BUILD_RE.search(text):
        return "build"
    if _SEARCH_RE.search(text):
        return "search"
    name = str(tool_name or "").lower()
    if name in ("edit", "write", "multiedit", "str_replace_editor"):
        return "edit"
    if name in ("read", "glob", "grep", "search", "codebase_search"):
        return "search"
    return "other"


@dataclass
class _Bucket:
    """一个时间盒桶。

    :param key: 桶的键。
    :param kind: 桶的类型（``cmd`` / ``cat``）。
    :param category: 操作类别。
    :param label: 给人看的标签。
    :param sample: 样例命令。
    """

    key: str
    kind: str
    category: str
    label: str
    sample: str = ""
    #: 首次出现的时间戳。
    first_seen: float = 0.0
    #: 最近一次出现的时间戳。
    last_seen: float = 0.0
    #: 出现次数。
    count: int = 0
    #: 已经告警过（同一件事只报一次，不刷屏）。
    fired: bool = False
    #: 该桶的历史（给告警当证据）。
    samples: List[str] = field(default_factory=list)


class TimeBoxDetector:
    """时间盒检测器。

    :param window_minutes: 时间盒长度（分钟）。
    :param min_repeats: 判定跑偏所需的最少重复次数。
    :param enabled: 是否启用。
    :param categories: 启用「同类堆积」判定的类别；空 = 全关。
    :param guardian_suspend: 超时后要不要让守护者挂起进程。

        * ``None``（默认）—— **跟着守护者的武装状态走**：
          守护者武装着（``enabled`` 且 ``intervention_mode=guardian``）才挂；
        * ``True`` —— 只要有守护者就挂；
        * ``False`` —— 永远不挂（只告警）。
    :param clock: 时钟（测试用）。
    """

    def __init__(
        self,
        *,
        window_minutes: float = DEFAULT_WINDOW_MINUTES,
        min_repeats: int = DEFAULT_MIN_REPEATS,
        enabled: bool = True,
        categories: Optional[List[str]] = None,
        guardian_suspend: Optional[bool] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.window_minutes = max(float(window_minutes or 0), 0.1)
        self.min_repeats = max(int(min_repeats or 1), 1)
        self.enabled = bool(enabled)
        if categories is None:
            categories = list(DEFAULT_CATEGORIES)
        self.categories = {
            str(item).strip().lower()
            for item in categories
            if str(item).strip()
        }
        self.guardian_suspend = guardian_suspend
        self._clock = clock
        self._buckets: Dict[str, _Bucket] = {}
        #: 已经报过的桶（同一件事只报一次）。
        self._fired: Dict[str, float] = {}

    def guardian_suspend_allowed(self, guardian: Any = None) -> bool:
        """能不能在撞破时间盒时**挂起**死磕的进程？

        ⚠️ 判定顺序
        ==========

        1. 配置显式 ``false`` → **永远不挂**（只告警）；
        2. 配置显式 ``true`` → 有守护者就挂；
        3. 配置**留空**（默认）→ **跟着守护者的武装状态走** ——
           守护者武装着（``enabled`` 且 ``intervention_mode=guardian``）才挂。

        为什么默认跟着守护者走
        ======================

        守护者本身就是「用户授权我去动手」的开关 ——
        用户把 ``intervention_mode`` 设成 ``guardian``，
        就等于说了「该拦的给我拦」。
        「超时死磕」也是一种该拦的情况，
        所以不必再要用户配一遍。

        :param guardian: 守护者实例（可能为 ``None``）。
        :return: 该不该挂起。
        """
        if self.guardian_suspend is False:
            return False
        if self.guardian_suspend is True:
            return guardian is not None
        if guardian is None:
            return False
        enabled = bool(getattr(guardian, "enabled", False))
        mode = str(getattr(guardian, "mode", "") or "").lower()
        return enabled and mode == "guardian"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def window_seconds(self) -> float:
        """时间盒长度（秒）。"""
        return self.window_minutes * 60.0

    def observe(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """喂入一个事件，判断是否撞破了时间盒。

        :param event: 统一事件字典。
        :return: 命中时返回**判定字典**，否则返回 ``None``。
        """
        if not self.enabled:
            return None
        if not isinstance(event, dict):
            return None
        if str(event.get("type") or "") != "tool_call":
            return None

        tool = event.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        tool_name = str(tool.get("name") or "")
        command = str(tool.get("command") or "")
        arguments = tool.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        if not command:
            command = str(arguments.get("command") or "")

        normalized = self._normalize(command)
        if not normalized or normalized in _IGNORED_COMMANDS:
            return None

        category = classify_operation(command, tool_name)
        now = self._clock()
        hits: List[_Bucket] = []

        # --- 桶 1：同一条命令反复出现 ---
        cmd_bucket = self._touch(
            "cmd:{}:{}".format(category, normalized),
            kind="cmd",
            category=category,
            label=normalized,
            sample=command,
            now=now,
        )
        if self._is_exceeded(cmd_bucket, now):
            hits.append(cmd_bucket)

        # --- 桶 2：同一类操作堆积（只有盯着的类别才看） ---
        cat_bucket = None
        if category in self.categories:
            cat_bucket = self._touch(
                "cat:{}".format(category),
                kind="cat",
                category=category,
                label=CATEGORY_LABELS.get(category, category),
                sample=command,
                now=now,
            )
            if self._is_exceeded(cat_bucket, now):
                hits.append(cat_bucket)

        if not hits:
            return None
        # ⚠️ 命中就**把相关桶一起标记** ——
        # 「同一条命令」和「同一类操作」经常同时成立，
        # 那是**一件事**，不该报两次警（刷屏）。
        cmd_bucket.fired = True
        if cat_bucket is not None:
            cat_bucket.fired = True
        # 报最具体的那个（命令桶比类别桶信息量大）。
        bucket = hits[0]
        return self._build_verdict(bucket, now)

    def state(self) -> Dict[str, Any]:
        """返回当前所有桶的快照（给看板 / 调试用）。

        :return: 快照字典。
        """
        return {
            "enabled": self.enabled,
            "window_minutes": self.window_minutes,
            "min_repeats": self.min_repeats,
            "categories": sorted(self.categories),
            "buckets": [
                {
                    "key": bucket.key,
                    "kind": bucket.kind,
                    "category": bucket.category,
                    "label": bucket.label,
                    "count": bucket.count,
                    "first_seen": bucket.first_seen,
                    "last_seen": bucket.last_seen,
                    "fired": bucket.fired,
                }
                for bucket in self._buckets.values()
            ],
        }

    def reset(self) -> None:
        """清空所有桶（会话结束 / 重新锚定目标时用）。

        :return: 无。
        """
        self._buckets.clear()
        self._fired.clear()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _touch(
        self,
        key: str,
        *,
        kind: str,
        category: str,
        label: str,
        sample: str,
        now: float,
    ) -> _Bucket:
        """记一次「又干了一遍同样的事」。

        :param key: 桶键。
        :param kind: 桶类型。
        :param category: 操作类别。
        :param label: 标签。
        :param sample: 样例命令。
        :param now: 当前时间戳。
        :return: 桶对象。
        """
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                key=key,
                kind=kind,
                category=category,
                label=label,
                sample=sample,
                first_seen=now,
                last_seen=now,
                count=0,
            )
            self._buckets[key] = bucket
        bucket.count += 1
        bucket.last_seen = now
        if sample and len(bucket.samples) < 5:
            bucket.samples.append(sample[:300])
        return bucket

    def _is_exceeded(self, bucket: _Bucket, now: float) -> bool:
        """这个桶是不是已经撞破了时间盒。

        :param bucket: 桶。
        :param now: 当前时间戳。
        :return: 是否超限。
        """
        if bucket.fired:
            return False
        if bucket.count < self.min_repeats:
            return False
        return (now - bucket.first_seen) >= self.window_seconds

    def _build_verdict(self, bucket: _Bucket, now: float) -> Dict[str, Any]:
        """把超限的桶变成一条**判定**。

        :param bucket: 超限的桶。
        :param now: 当前时间戳。
        :return: 判定字典。
        """
        self._fired[bucket.key] = now
        minutes = (now - bucket.first_seen) / 60.0
        if bucket.kind == "cmd":
            detail = "同一条命令「{}」重复了 {} 次".format(
                bucket.label, bucket.count
            )
        else:
            detail = "「{}」这类操作累计了 {} 次".format(
                CATEGORY_LABELS.get(bucket.category, bucket.category),
                bucket.count,
            )
        reason = (
            "{}：{}，已经耗时 {:.1f} 分钟（时间盒 {:.1f} 分钟）。"
            "子任务在吃掉整轮 —— 先停一停，确认它还在做主线任务。"
        ).format(
            TIMEBOX_ALERT_REASON,
            detail,
            minutes,
            self.window_minutes,
        )
        evidence = "\n".join(bucket.samples) or bucket.sample
        return {
            "verdict": VERDICT_TIMEBOX,
            "reason": reason,
            "rule_id": RULE_ID_TIMEBOX,
            "evidence": evidence[:500],
            "level": "critical",
            "confidence": 0.85,
            "severity": "standard",
            "category": "timebox",
            "timebox": {
                "kind": bucket.kind,
                "operation_category": bucket.category,
                "label": bucket.label,
                "count": bucket.count,
                "minutes": round(minutes, 1),
                "window_minutes": self.window_minutes,
            },
        }

    @staticmethod
    def _normalize(command: str) -> str:
        """把命令归一化成**比较键**。

        统一小写、压缩空白 —— ``pytest  a.py`` 与 ``pytest a.py``
        应当算同一条命令。

        :param command: 原始命令。
        :return: 归一化后的命令。
        """
        return _WHITESPACE.sub(" ", str(command or "").strip().lower())


def build_timebox(config: Dict[str, Any]) -> Optional[TimeBoxDetector]:
    """按配置造一个时间盒检测器。

    :param config: 哨兵配置。
    :return: 检测器；不该建时返回 ``None``。
    """
    rules = (config or {}).get("rules") or {}
    timebox_config = rules.get("timebox") or {}
    if not bool(timebox_config.get("enabled", True)):
        return None
    categories = timebox_config.get("categories")
    if categories is None:
        categories = list(DEFAULT_CATEGORIES)
    raw_suspend = timebox_config.get("guardian_suspend", None)
    if raw_suspend is None:
        suspend: Optional[bool] = None
    else:
        suspend = bool(raw_suspend)
    return TimeBoxDetector(
        window_minutes=float(
            timebox_config.get("window_minutes") or DEFAULT_WINDOW_MINUTES
        ),
        min_repeats=int(timebox_config.get("min_repeats") or DEFAULT_MIN_REPEATS),
        enabled=True,
        categories=list(categories),
        guardian_suspend=suspend,
    )
