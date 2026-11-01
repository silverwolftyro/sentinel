"""夜间值守晨报 —— 「早上打开电脑，第一眼看到昨晚发生了什么」。

为什么要有它
============

半夜挂机的真实痛点不是「有没有告警」，而是：

> 早上打开电脑，面对**一夜的几百条日志**，
> 要自己拼出「昨晚到底发生了什么、烧了多少、拦住了几次」。

这份晨报就是那个「拼」的动作 —— 哨兵替你拼好，直接给你一份 Markdown。

晨报里有什么
============

============================ ==========================================
条目                         数据来源
============================ ==========================================
执行时长                     引擎的 ``started_at`` / ``ended_at``
总 Token 消耗               引擎的 Token 台账（有 usage 用真值，否则估算）
拦截的红线次数               告警里 ``severity == "red_line"`` 的条数
意图漂移次数                 告警里 ``verdict == "DRIFT"`` 的条数
``[STUCK]`` 次数             告警里 ``verdict == "STUCK"`` 的条数
预估挽回的 Token 浪费        告警里 ``token_saved`` 的累加
本次用了哪些模型             L2 路由器的 ``models_used``
============================ ==========================================

三条原则
========

1. **不撒谎。** 估算值必须**标出来**（``~`` 前缀），
   绝不把估算说成账单 —— 这与 v1.1.0 的 Token 计数器口径一致；
2. **不添油加醋。** 晨报只报**发生过**的事，
   没有拦截就说「没有」，不写「守护神时刻」这种废话；
3. **不依赖模型。** 晨报的**统计部分**是纯计算，
   模型不可用也照样能生成 —— 只有「综述」那段可能需要模型。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SEVERITY_RED_LINE",
    "VERDICT_DRIFT",
    "VERDICT_LEAK",
    "VERDICT_STUCK",
    "BriefingStats",
    "briefing_filename",
    "build_briefing_summary",
    "collect_from_engine",
    "default_archive_dir",
    "render_markdown",
    "render_terminal",
    "save_briefing",
]

#: 红线级的 severity 取值。
SEVERITY_RED_LINE = "red_line"

#: 意图漂移判定。
VERDICT_DRIFT = "DRIFT"

#: 上下文卡死判定。
VERDICT_STUCK = "STUCK"

#: 敏感数据外泄判定。
VERDICT_LEAK = "LEAK"

#: 晨报归档目录（相对项目根）。
ARCHIVE_SUBDIR = os.path.join("_管理文档", "文档与报告", "晨报归档")


@dataclass
class BriefingStats:
    """晨报要用到的全部统计。

    这是一个**纯数据**结构 —— 它不关心数据从哪来，
    也不关心怎么渲染（那是 :func:`render_markdown` 的事）。
    这样统计、渲染、落盘三件事可以各自独立测试。

    :param session_id:  会话 ID。
    :param agent:      被监控的 Agent 类型。
    :param task_goal:  本次任务的目标（意图锚点）。
    :param started_at: 会话开始时间戳。
    :param ended_at:   会话结束时间戳。
    :param total_events: 处理过的事件总数。
    :param counts:     各判定的计数（``ON_TRACK`` / ``DANGEROUS`` / ``LOOP`` …）。
    :param total_tokens: 累计 Token 总量。
    :param estimated_tokens: 其中属于**估算**的部分。
    :param token_estimated: 总量本身是不是估算值。
    :param red_line_count:   红线级拦截次数。
    :param drift_count:      意图漂移次数。
    :param stuck_count:      ``[STUCK]`` 次数。
    :param leak_count:       ``[LEAK]`` 次数。
    :param loop_count:       死循环（``LOOP``）次数。
    :param token_saved:      预估挽回的 Token 浪费。
    :param models_used:      用过的模型 → 调用次数。
    :param escalations:      升级到强模型的记录。
    :param llm_degraded_calls: L2 判定**降级**（模型不可用）的次数。
    :param llm_degraded_reason: 最近一次降级的原因。
    :param alerts:           告警摘要（只留必要的字段）。
    """

    session_id: str = ""
    agent: str = ""
    task_goal: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    total_events: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    total_tokens: int = 0
    estimated_tokens: int = 0
    token_estimated: bool = True
    red_line_count: int = 0
    drift_count: int = 0
    stuck_count: int = 0
    leak_count: int = 0
    loop_count: int = 0
    token_saved: int = 0
    models_used: Dict[str, int] = field(default_factory=dict)
    escalations: List[Dict[str, Any]] = field(default_factory=list)
    llm_degraded_calls: int = 0
    llm_degraded_reason: str = ""
    alerts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """会话时长（秒）；没记录起止时间时返回 0。"""
        if self.started_at <= 0 or self.ended_at <= 0:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)

    @property
    def duration_text(self) -> str:
        """把时长渲染成人话（``2 小时 13 分``）。"""
        seconds = int(self.duration_seconds)
        if seconds <= 0:
            return "未知"
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        parts: List[str] = []
        if hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分")
        if not hours and secs:
            parts.append(f"{secs} 秒")
        return " ".join(parts) or "不到 1 秒"

    @property
    def token_text(self) -> str:
        """把 Token 总量渲染成人话；估算值带 ``~``。"""
        return format_token_count(
            self.total_tokens, estimated=self.token_estimated
        )


def format_token_count(value: int, *, estimated: bool = False) -> str:
    """把 Token 数量渲染成带「万」的中文数字。

    :param value:    Token 数量。
    :param estimated: 是不是估算值（估算值前缀加 ``~``）。
    :return: 形如 ``24.7 万`` / ``~539``。
    """
    count = max(0, int(value or 0))
    prefix = "~" if estimated else ""
    if count >= 10000:
        return f"{prefix}{count / 10000:.1f} 万"
    return f"{prefix}{count}"


def collect_from_engine(
    engine: Any,
    *,
    session_id: str = "",
    agent: str = "",
    task_goal: str = "",
    ended_at: Optional[float] = None,
) -> BriefingStats:
    """从引擎里采集晨报所需的统计。

    ⚠️ 这个函数**只读**引擎的状态 —— 它不修改任何东西，
    也不会去碰模型（万一引擎正在写，也只是读个快照）。

    :param engine:     :class:`~src.core.engine.SentinelEngine` 实例。
    :param session_id: 会话 ID（覆盖引擎里的）。
    :param agent:      Agent 类型（覆盖引擎里的）。
    :param task_goal:  任务目标（覆盖引擎里的）。
    :param ended_at:   结束时间戳；``None`` 时用当前时间。
    :return: 统计结果。
    """
    stats = BriefingStats()
    if engine is None:
        return stats

    stats.session_id = str(
        session_id or getattr(engine, "agent_name", "") or ""
    )
    stats.agent = str(agent or getattr(engine, "agent_name", "") or "")

    tracker = getattr(engine, "intent_tracker", None)
    goal = str(task_goal or "")
    if not goal and tracker is not None:
        goal = str(getattr(tracker, "goal", "") or "")
    stats.task_goal = goal

    stats.started_at = float(getattr(engine, "started_at", 0.0) or 0.0)
    stats.ended_at = float(
        ended_at if ended_at is not None else getattr(engine, "ended_at", 0.0) or 0.0
    )
    if stats.ended_at <= 0:
        stats.ended_at = time.time()

    stats.total_events = int(getattr(engine, "processed_events", 0) or 0)
    raw_counts = getattr(engine, "counts", None)
    if isinstance(raw_counts, dict):
        stats.counts = {
            str(key): int(value or 0) for key, value in raw_counts.items()
        }

    # --- Token ---
    ledger = getattr(engine, "token_ledger", None)
    snapshot: Dict[str, Any] = {}
    if ledger is not None:
        try:
            snapshot = ledger.snapshot()
        except Exception as exc:  # noqa: BLE001 —— 取不到不影响晨报
            logger.warning("读取 Token 台账失败：%s: %s", type(exc).__name__, exc)
    if isinstance(snapshot, dict):
        stats.total_tokens = int(snapshot.get("total_tokens") or 0)
        stats.estimated_tokens = int(snapshot.get("estimated_tokens") or 0)
        stats.token_estimated = bool(snapshot.get("estimated", True))

    # --- 告警统计 ---
    alerts = getattr(engine, "alerts", None)
    if isinstance(alerts, list):
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            severity = str(alert.get("severity") or "")
            verdict = str(alert.get("verdict") or "")
            if severity == SEVERITY_RED_LINE:
                stats.red_line_count += 1
            if verdict == VERDICT_DRIFT:
                stats.drift_count += 1
            if verdict == VERDICT_STUCK:
                stats.stuck_count += 1
            if verdict == VERDICT_LEAK:
                stats.leak_count += 1
            if verdict == "LOOP":
                stats.loop_count += 1
            saved = alert.get("token_saved")
            if isinstance(saved, dict):
                stats.token_saved += int(saved.get("tokens") or 0)
            stats.alerts.append(
                {
                    "verdict": verdict,
                    "level": str(alert.get("level") or ""),
                    "rule_id": str(alert.get("rule_id") or ""),
                    "reason": str(alert.get("reason") or ""),
                    "severity": severity,
                    "timestamp": float(alert.get("timestamp") or 0.0),
                    "explanation": str(alert.get("explanation") or ""),
                    "token_saved": int(
                        (saved or {}).get("tokens") or 0
                    )
                    if isinstance(saved, dict)
                    else 0,
                }
            )

    # --- 用过的模型（L2 路由器的记账）---
    llm_client = getattr(engine, "llm_client", None)
    models_used = getattr(llm_client, "models_used", None)
    if isinstance(models_used, dict):
        stats.models_used = {
            str(key): int(value or 0) for key, value in models_used.items()
        }
    escalations = getattr(llm_client, "escalations", None)
    if isinstance(escalations, list):
        stats.escalations = [item for item in escalations if isinstance(item, dict)]
    stats.llm_degraded_calls = int(
        getattr(llm_client, "degraded_calls", 0) or 0
    )
    stats.llm_degraded_reason = str(
        getattr(llm_client, "last_degraded_reason", "") or ""
    )

    return stats


def render_markdown(
    stats: BriefingStats,
    *,
    title: str = "哨兵值守晨报",
    heading_level: int = 1,
) -> str:
    """把统计渲染成 Markdown 晨报。

    :param stats: 统计结果。
    :param title: 文档标题。
    :param heading_level: 标题级别（1 = 独立文档；2 = 合并日报里的分节）。
    :return: Markdown 文本（以 ``\\n`` 结尾）。
    """
    prefix = "#" * max(1, int(heading_level))
    sub = "#" * (max(1, int(heading_level)) + 1)
    sub2 = "#" * (max(1, int(heading_level)) + 2)
    now = datetime.now()
    lines: List[str] = [
        f"{prefix} {title}",
        "",
        f"> 生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    if stats.session_id:
        lines.append(f"- **会话**：{stats.session_id}")
    if stats.agent:
        lines.append(f"- **Agent**：{stats.agent}")
    if stats.task_goal:
        lines.append(f"- **任务目标**：{stats.task_goal}")
    lines.append(f"- **执行时长**：{stats.duration_text}")
    lines.append(f"- **处理事件**：{stats.total_events} 个")
    lines.append("")

    # --- 一句话结论 ---
    lines.append(f"{sub} 一句话结论")
    lines.append("")
    lines.append(_verdict_sentence(stats))
    lines.append("")

    # --- 核心数字 ---
    lines.append(f"{sub} 关键数字")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 总 Token 消耗 | {stats.token_text} |")
    lines.append(f"| 红线级拦截 | {stats.red_line_count} 次 |")
    lines.append(f"| 意图漂移 | {stats.drift_count} 次 |")
    lines.append(f"| `[STUCK]` 卡死 | {stats.stuck_count} 次 |")
    lines.append(f"| `[LEAK]` 外泄 | {stats.leak_count} 次 |")
    lines.append(f"| 死循环 `LOOP` | {stats.loop_count} 次 |")
    lines.append(
        f"| **预估挽回的无效消耗** | "
        f"**{format_token_count(stats.token_saved)} Token** |"
    )
    lines.append("")

    on_track = int(stats.counts.get("ON_TRACK", 0) or 0)
    dangerous = int(stats.counts.get("DANGEROUS", 0) or 0)
    loop = int(stats.counts.get("LOOP", 0) or 0)
    lines.append("判定分布：")
    lines.append("")
    lines.append(f"- 正常 `ON_TRACK`：{on_track}")
    lines.append(f"- 危险 `DANGEROUS`：{dangerous}")
    lines.append(f"- 死循环 `LOOP`：{loop}")
    lines.append("")

    # --- 模型 ---
    lines.append(f"{sub} 本次用到的模型")
    lines.append("")
    if stats.models_used:
        for name, count in sorted(
            stats.models_used.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"- `{name}` —— 调用 {count} 次")
    else:
        lines.append("- （本次会话没有调用任何模型 —— 规则层单独工作）")
    if stats.escalations:
        lines.append("")
        lines.append("升级到强模型的时刻：")
        lines.append("")
        for item in stats.escalations:
            reason = str(item.get("reason") or "")
            label = {
                "red_line": "红线级拦截",
                "drift_streak": "连续意图漂移",
            }.get(reason, reason or "未知原因")
            stamp = ""
            at = float(item.get("at") or 0.0)
            if at > 0:
                stamp = datetime.fromtimestamp(at).strftime("%H:%M:%S")
            lines.append(
                f"- {stamp} —— {label} → `{item.get('model') or '未知模型'}`"
            )
    if stats.llm_degraded_calls:
        # ⚠️ 静悄悄的失效最坏 —— 晨报必须**如实**说：有一段时间模型判不了，
        # 那段时间的漂移判定全是「按未漂移处理」，等于没开。
        lines.append("")
        lines.append(
            f"⚠️ 有 **{stats.llm_degraded_calls}** 次 L2 判定因模型不可用而降级"
            "（一律按「未漂移」处理，红线 R4）——"
            "**那段时间的意图漂移检测等于没开**。"
        )
        if stats.llm_degraded_reason:
            lines.append("")
            lines.append(f"  最近一次原因：{stats.llm_degraded_reason}")
    lines.append("")

    # --- 明细 ---
    lines.append(f"{sub} 告警明细")
    lines.append("")
    if stats.alerts:
        lines.append("| 时间 | 判定 | 规则 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for alert in stats.alerts:
            stamp = ""
            at = float(alert.get("timestamp") or 0.0)
            if at > 0:
                stamp = datetime.fromtimestamp(at).strftime("%H:%M:%S")
            verdict = str(alert.get("verdict") or "")
            if alert.get("severity") == SEVERITY_RED_LINE:
                verdict = f"{verdict}（红线级）"
            reason = str(alert.get("reason") or "").replace("|", "\\|")
            lines.append(
                f"| {stamp} | {verdict} | `{alert.get('rule_id') or '-'}` "
                f"| {reason} |"
            )
    else:
        lines.append("（整晚零告警 —— Agent 老老实实干完了活）")
    lines.append("")

    # --- 解释摘录 ---
    explained = [
        alert
        for alert in stats.alerts
        if str(alert.get("explanation") or "").strip()
    ]
    if explained:
        lines.append(f"{sub} 模型解读摘录")
        lines.append("")
        for alert in explained:
            stamp = ""
            at = float(alert.get("timestamp") or 0.0)
            if at > 0:
                stamp = datetime.fromtimestamp(at).strftime("%H:%M:%S")
            lines.append(f"{sub2} {stamp} {alert.get('rule_id') or ''}")
            lines.append("")
            lines.append(str(alert.get("explanation") or "").strip())
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "本晨报由哨兵自动生成；Token 数值为**统计值**"
        + ("（含估算部分）。" if stats.token_estimated else "（来自模型真实上报）。")
    )
    lines.append("")
    return "\n".join(lines)


def build_briefing_summary(
    stats: BriefingStats,
    *,
    title: str = "",
) -> Dict[str, Any]:
    """把晨报统计压缩成一份**短摘要** —— 给手机推送用。

    为什么要有「摘要」
    ===============

    完整晨报是一份 Markdown 文档（几 KB 到几十 KB）。
    整篇塞进钉钉 / 飞书的消息体，很容易**被截断** ——
    用户在手机上看到的是一段莫名其妙断掉的话，
    一点也拼不出「昨晚到底拦住了什么」。

    所以推送的是**提炼过的核心数据**（任务书点名的三项）：

    * **总拦截次数**（``DANGEROUS`` 判定数）；
    * **红线次数**（``red_line_count``）；
    * **挽回 Token 数**（``token_saved``）。

    外加两个「值守体检」数字：值守时长与处理事件数 ——
    一屏之内能读完，用户一眼知道「这一夜值不值」。

    ⚠️ 这个函数**不认识任何通道** —— 它只产出纯数据。
    报文格式由 :mod:`src.core.channels` 那边负责
    （摘要与告警是两种东西，格式不一样）。

    :param stats: 统计结果。
    :param title: 标题；留空则用「哨兵晨报 · 日期」。
    :return: ``{"title", "markdown", "text", "stats"}`` 四个键。
    """
    now = datetime.now()
    heading = title or "哨兵晨报 · {}".format(now.strftime("%Y-%m-%d"))
    on_track = int(stats.counts.get("ON_TRACK", 0) or 0)
    dangerous = int(stats.counts.get("DANGEROUS", 0) or 0)
    loop = int(stats.counts.get("LOOP", 0) or 0)

    # --- 核心三项（任务书要求）-----------------------------------------
    intercepts = dangerous
    red_line = int(stats.red_line_count or 0)
    token_saved = int(stats.token_saved or 0)

    detail = stats.duration_text
    lines = [
        "## 🌅 {}".format(heading),
        "",
        "⏱ **值守 {}** · 处理 {} 条事件".format(detail, stats.total_events),
        "",
        "🚦 **拦截 {} 次**（红线 {} 次）".format(intercepts, red_line),
        "💾 **挽回 Token {}**".format(format_token_count(token_saved)),
    ]
    if stats.drift_count or stats.stuck_count:
        lines.append(
            "🧭 意图漂移 {} 次 · `[STUCK]` 卡死 {} 次".format(
                stats.drift_count, stats.stuck_count
            )
        )
    if stats.llm_degraded_calls:
        # ⚠️ 推给手机的那条消息也不能粉饰 —— 模型判不了就得说。
        lines.append(
            "⚠️ 有 {} 次 L2 判定降级（模型不可用）—— "
            "那段时间的漂移检测等于没开".format(stats.llm_degraded_calls)
        )
    if stats.task_goal:
        lines.extend(["", "> 任务：{}".format(stats.task_goal)])

    text_lines = [
        "🌅 {}".format(heading),
        "值守 {} · 处理 {} 条事件".format(detail, stats.total_events),
        "拦截 {} 次（红线 {} 次）".format(intercepts, red_line),
        "挽回 Token {}".format(format_token_count(token_saved)),
    ]

    return {
        "title": "🌅 {}".format(heading),
        "markdown": "\n".join(lines),
        "text": "\n".join(text_lines),
        "stats": {
            "session_id": stats.session_id,
            "agent": stats.agent,
            "task_goal": stats.task_goal,
            "started_at": stats.started_at,
            "ended_at": stats.ended_at,
            "duration_seconds": int(stats.duration_seconds),
            "duration_text": detail,
            "total_events": int(stats.total_events),
            # --- 核心三项 ---
            "intercepts": intercepts,
            "red_line": red_line,
            "token_saved": token_saved,
            # --- 补充 ---
            "drift": int(stats.drift_count or 0),
            "stuck": int(stats.stuck_count or 0),
            "leak": int(stats.leak_count or 0),
            "loop": loop,
            "on_track": on_track,
            "total_tokens": int(stats.total_tokens or 0),
            "token_estimated": bool(stats.token_estimated),
            # --- 诚实项：模型判不了多少次 ---
            "llm_degraded": int(stats.llm_degraded_calls or 0),
            "llm_degraded_reason": str(stats.llm_degraded_reason or ""),
        },
    }


def _verdict_sentence(stats: BriefingStats) -> str:
    """生成「一句话结论」。

    :param stats: 统计结果。
    :return: 一句话。
    """
    if stats.red_line_count:
        return (
            f"昨晚拦下 **{stats.red_line_count}** 次红线级操作，"
            f"预估挽回 {format_token_count(stats.token_saved)} Token 的无效消耗。"
        )
    if stats.drift_count or stats.stuck_count:
        return (
            f"昨晚没有红线级拦截，但出现 **{stats.drift_count}** 次意图漂移、"
            f"**{stats.stuck_count}** 次 `[STUCK]` 卡死 —— "
            "建议看一下告警明细里的模型解读。"
        )
    if stats.alerts:
        return (
            f"昨晚共 {len(stats.alerts)} 条告警，**没有**红线级拦截，"
            "Agent 总体在轨道上。"
        )
    return "昨晚整晚零告警 —— Agent 老实干活，一夜平安。"


def render_terminal(stats: BriefingStats) -> str:
    """把晨报渲染成**终端摘要**（几行看完）。

    :param stats: 统计结果。
    :return: 多行文本。
    """
    lines = [
        "",
        "=" * 62,
        "  哨兵 · 夜间值守晨报",
        "=" * 62,
        f"  会话      ：{stats.session_id or '（未命名）'}"
        + (f" · {stats.agent}" if stats.agent else ""),
        f"  时长      ：{stats.duration_text}（{stats.total_events} 个事件）",
        f"  Token     ：{stats.token_text}",
        f"  红线拦截  ：{stats.red_line_count} 次",
        f"  意图漂移  ：{stats.drift_count} 次",
        f"  [STUCK]   ：{stats.stuck_count} 次",
        f"  预估挽回  ：{format_token_count(stats.token_saved)} Token",
    ]
    if stats.models_used:
        parts = [
            f"{name} ×{count}"
            for name, count in sorted(
                stats.models_used.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]
        lines.append(f"  用到模型  ：{'、'.join(parts)}")
    lines.append(f"  结论      ：{_verdict_sentence(stats)}")
    lines.append("=" * 62)
    lines.append("")
    return "\n".join(lines)


def default_archive_dir() -> str:
    """晨报归档目录的默认位置。

    源码运行时是「项目根 / ``_管理文档/文档与报告/晨报归档/``」；
    打包成 exe 后是「exe 所在目录 / ``晨报归档/``」——
    单文件程序没有「项目根」的概念，跟着 exe 走最不容易出错。

    :return: 归档目录路径。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        core_dir = os.path.dirname(os.path.abspath(__file__))
        base = os.path.dirname(os.path.dirname(core_dir))
    return os.path.join(base, ARCHIVE_SUBDIR)


def briefing_filename(when: Optional[datetime] = None) -> str:
    """生成晨报文件名（``晨报-YYYY-MM-DD.md``）。

    :param when: 时间；``None`` 时用当前时间。
    :return: 文件名。
    """
    moment = when or datetime.now()
    return f"晨报-{moment.strftime('%Y-%m-%d')}.md"


def _session_markers(session_id: str) -> tuple:
    """生成一路会话在「每日合并日报」里的标记对。

    :param session_id: 会话 ID。
    :return: ``(起始标记, 结束标记)``。
    """
    safe = str(session_id or "default").replace("]", ")").replace("\n", " ")
    return (
        f"<!-- sentinel-session:{safe} -->",
        f"<!-- /sentinel-session:{safe} -->",
    )


#: 晨报分节的匹配模式。
#:
#: 一路会话在日报里长这样：起始标记 → 正文 → 结束标记。
#: 正文用非贪婪匹配，所以遇到**第一个**结束标记就收尾；
#: 结束标记不要求与起始标记同名 —— 宁可多收一点，
#: 也不要把两路会话的内容粘成一大坨。
_SECTION_PATTERN = re.compile(
    r"<!--\s*sentinel-session:(?P<sid>[^\n]*?)\s*-->"
    r"(?P<body>.*?)"
    r"<!--\s*/sentinel-session:[^\n]*?\s*-->",
    re.DOTALL,
)


def _extract_sections(content: str) -> List[str]:
    """从已有晨报里**解析出各个会话分节**。

    为什么要「解析」而不是「在原文上追加」
    =====================================

    早期版本的晨报没有会话标记（一份文件就是一份晨报）。
    如果直接往原文后面追加新分节，文件里就会同时存在
    「旧格式的一级标题」和「新格式的二级分节」，
    两套标题层级混在一起，人看着别扭。

    这里的做法是：**只认标记之间的内容**，
    标记之外的一律视为旧格式残留，丢弃。
    重建出来的日报因此永远是**同一个格式**。

    :param content: 已有文件内容（可以为空）。
    :return: 分节列表，每一项都**包含**自己的起止标记。
    """
    if not content:
        return []
    sections: List[str] = []
    for match in _SECTION_PATTERN.finditer(content):
        section = match.group(0).strip()
        if section:
            sections.append(section)
    return sections


def save_briefing(
    markdown: str,
    *,
    archive_dir: Optional[str] = None,
    when: Optional[datetime] = None,
    session_id: str = "",
) -> str:
    """把晨报落盘。

    落盘规则分两种：

    * **单路会话**（``session_id`` 为空）—— 直接覆盖同名文件。
      同一天重跑是**刷新**而不是堆积
      （不产生 ``晨报-2026-10-27(2).md`` 这种东西）；
    * **多路会话**（给了 ``session_id``）—— 同一天的几路**合并**进
      同一份「日报」，每一路用 HTML 注释标记分节。
      这样守了四路夜，第二天看到的是**一份**完整的日报，
      而不是只剩最后一路的晨报。
      同一路重跑时会**替换自己那一节**，不会重复堆叠。

    :param markdown:    Markdown 正文。
    :param archive_dir: 归档目录；``None`` 时用默认位置。
    :param when:        时间；``None`` 时用当前时间。
    :param session_id:  会话 ID；给了就走「合并日报」模式。
    :return: 落盘后的**绝对路径**。
    :raises OSError: 目录创建或写文件失败。
    """
    directory = archive_dir or default_archive_dir()
    os.makedirs(directory, exist_ok=True)
    moment = when or datetime.now()
    path = os.path.join(directory, briefing_filename(moment))

    if not session_id:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        logger.info("晨报已保存：%s", path)
        return os.path.abspath(path)

    # --- 多路：合并进每日日报 -------------------------------------------
    start, end = _session_markers(session_id)
    existing = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
        except OSError as exc:  # noqa: BLE001 —— 读不到就当空文件重写
            logger.warning("读取已有晨报失败，将重写：%s: %s", type(exc).__name__, exc)
            existing = ""

    # 把已有文件里的**各个会话分节**重新解析出来。
    #
    # 为什么要「重新解析」而不是「在原文上追加」：
    # 早期版本的晨报没有会话标记，直接追加会让文件里
    # 出现「一半旧格式、一半新格式」的两套标题层级。
    # 解析出分节再重建，旧格式残留就自然被丢掉了。
    sections = _extract_sections(existing)

    section = f"{start}\n{markdown.strip()}\n{end}"
    replaced = False
    for index, item in enumerate(sections):
        if item.startswith(start):
            sections[index] = section
            replaced = True
            break
    if not replaced:
        sections.append(section)

    merged = f"# 哨兵值守晨报 · {moment.strftime('%Y-%m-%d')}\n\n"
    merged += "\n\n---\n\n".join(sections) + "\n"

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(merged)
    logger.info("晨报已写入每日日报：%s（会话 %s）", path, session_id)
    return os.path.abspath(path)
