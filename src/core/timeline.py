"""案发时间线复盘 —— 早上看到 Diff 时，您想知道「它是怎么一步步走到这一步的」。

问题从哪来
==========

v2.1.0 有了【查看改动】，您能看到 Agent 改了哪些文件。
但**改了什么**和**怎么走到这一步**是两回事：

* 它是在第 3 分钟就偏了，还是第 40 分钟才偏的？
* 那次红线拦截之前，它已经连着跑失败 5 次了吗？
* 中间我手动点过【回滚并重试】吗？点了之后它老实了吗？

挂一整夜的用户早上面对的是**一屏告警**，不是一条故事线。
所以这里把散落在各处的事实重新拼成一条**带时间戳的线**。

时间线的数据来源
================

四份日志，四类节点：

====================== ==========================================
来源                    产出什么节点
====================== ==========================================
``EVENT_JSON`` 事件行   操作节点（开始目标 / 复活重试 / 回滚…）
``ALERT_JSON`` 告警行   异常节点（漂移 / 卡死 / 红线拦截…）
安全点服务               快照节点（改动的回滚基线）
（时间本身）             相邻节点的**间隔**（「隔了 12 分钟」）
====================== ==========================================

「隔了多久」是这里最值钱的东西 ——
用户问的其实是「**在触发拦截的 10 分钟前，Agent 到底在干什么**」，
而那个答案不在某一条日志里，在**两条日志的时间差**里。

设计约束
========

1. **纯函数** —— :func:`build_timeline` 只吃字典、只吐字典，
   不读文件、不碰网络。这样它既能看板用，也能单测用。
2. **绝不抛异常** —— 复盘功能不该挡住任何事（架构红线 R5）。
3. **不撒谎** —— 没有安全点就明说没有，不编一个假的出来。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 节点类型
# ---------------------------------------------------------------------------

#: 开始目标 —— 会话一开始用户交代的任务。
NODE_GOAL = "goal"
#: 意图漂移 —— Agent 开始做不是用户要的事。
NODE_DRIFT = "drift"
#: 红线拦截 —— 高危命令被挂起（真出事了）。
NODE_REDLINE = "redline"
#: 上下文卡死 —— 连续失败。
NODE_STUCK = "stuck"
#: 疑似死循环。
NODE_LOOP = "loop"
#: 敏感数据外泄。
NODE_LEAK = "leak"
#: 其他告警。
NODE_ALERT = "alert"
#: 操作节点 —— 复活、回滚、注入、热重载……
NODE_ACTION = "action"
#: 安全点快照 —— 改动可以回滚到的基线。
NODE_SAFE_POINT = "safe_point"

#: 节点严重程度。
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

#: 判定 → 节点类型。
_VERDICT_NODES = {
    "DRIFT": (NODE_DRIFT, SEVERITY_WARN, "意图漂移"),
    "STUCK": (NODE_STUCK, SEVERITY_WARN, "上下文卡死"),
    "LOOP": (NODE_LOOP, SEVERITY_WARN, "疑似死循环"),
    "LEAK": (NODE_LEAK, SEVERITY_CRITICAL, "敏感数据外泄"),
    "DANGEROUS": (NODE_REDLINE, SEVERITY_CRITICAL, "高危命令"),
}


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
@dataclass
class TimelineNode:
    """时间线上的一个节点。

    :param ts: 时间戳（epoch 秒）。
    :param kind: 节点类型（见 ``NODE_*`` 常量）。
    :param title: 一句话标题。
    :param detail: 补充说明。
    :param severity: 严重程度（``info`` / ``warn`` / ``critical``）。
    :param alert_id: 关联的告警 ID（有的话）。
    :param safe_point_id: 关联的**安全点** ID（有的话）。
    :param point_id: 算 Diff 用的**回滚基线** ID。
    :param extra: 附加信息（规则 ID、命令原文…）。
    """

    ts: float = 0.0
    kind: str = NODE_ALERT
    title: str = ""
    detail: str = ""
    severity: str = SEVERITY_INFO
    alert_id: str = ""
    safe_point_id: str = ""
    point_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成字典（给 JSON 接口用）。

        :return: 节点字典。
        """
        return {
            "ts": float(self.ts),
            "time": format_clock(self.ts),
            "datetime": format_datetime(self.ts),
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "alert_id": self.alert_id,
            "safe_point_id": self.safe_point_id,
            "point_id": self.point_id,
            "has_diff": bool(self.point_id),
            "extra": dict(self.extra),
        }


def format_clock(ts: float) -> str:
    """把时间戳格式化成 ``HH:MM:SS``。

    :param ts: epoch 秒。
    :return: 时间字符串；时间戳无效时返回空串。
    """
    try:
        value = float(ts or 0.0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(value))


def format_datetime(ts: float) -> str:
    """把时间戳格式化成 ``MM-DD HH:MM:SS``。

    :param ts: epoch 秒。
    :return: 时间字符串；时间戳无效时返回空串。
    """
    try:
        value = float(ts or 0.0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(value))


def format_gap(seconds: float) -> str:
    """把「两个节点隔了多久」说成人话。

    :param seconds: 间隔秒数。
    :return: ``12 秒`` / ``3 分 20 秒`` / ``1 小时 05 分`` 之类。
    """
    try:
        value = max(float(seconds or 0.0), 0.0)
    except (TypeError, ValueError):
        return ""
    if value < 60:
        return "{:.0f} 秒".format(value)
    if value < 3600:
        return "{:.0f} 分 {:.0f} 秒".format(value / 60.0, value % 60)
    return "{:.0f} 小时 {:.0f} 分".format(value / 3600.0, (value % 3600) / 60.0)


# ---------------------------------------------------------------------------
# 节点来源
# ---------------------------------------------------------------------------
def _safe_point_refs(alert: Dict[str, Any]) -> Dict[str, str]:
    """从告警里抠出安全点 ID。

    告警里的 ``safe_point`` 是**拦截那一刻**打的快照；
    真正能回滚到的基线是它的 ``rollback_point``（通常是会话基线）。

    :param alert: 告警字典。
    :return: ``{"safe_point_id": …, "point_id": …}``。
    """
    result = {"safe_point_id": "", "point_id": ""}
    try:
        point = alert.get("safe_point") or {}
        if not isinstance(point, dict):
            return result
        result["safe_point_id"] = str(point.get("id") or "")
        rollback = point.get("rollback_point") or {}
        if isinstance(rollback, dict):
            result["point_id"] = str(rollback.get("id") or "")
    except Exception:  # noqa: BLE001 —— 取不到就当没有
        return {"safe_point_id": "", "point_id": ""}
    return result


def _node_from_alert(alert: Dict[str, Any]) -> Optional[TimelineNode]:
    """把一条告警变成一个时间线节点。

    :param alert: 告警字典。
    :return: 节点；不是有效告警时返回 ``None``。
    """
    if not isinstance(alert, dict):
        return None
    verdict = str(alert.get("verdict") or "").upper()
    severity = str(alert.get("severity") or "")
    level = str(alert.get("level") or "").lower()

    kind, node_severity, label = _VERDICT_NODES.get(
        verdict, (NODE_ALERT, SEVERITY_WARN, "告警")
    )
    # ⚠️ 「高危命令」只有到**红线级**才算真的被拦下来了；
    # 普通级的高危命令只是提示，不该在时间线上摆出红线的架子。
    if severity == "red_line":
        kind = NODE_REDLINE
        node_severity = SEVERITY_CRITICAL
        label = "红线拦截"
    elif verdict == "DANGEROUS":
        kind = NODE_ALERT
        node_severity = SEVERITY_WARN
        label = "高危命令"
    elif level == "critical":
        node_severity = SEVERITY_CRITICAL

    reason = str(alert.get("reason") or "").strip()
    command = str(alert.get("command") or "").strip()
    detail = reason or label
    if command and command not in detail:
        detail = "{} —— {}".format(detail, command[:120])

    refs = _safe_point_refs(alert)
    return TimelineNode(
        ts=float(alert.get("timestamp") or 0.0),
        kind=kind,
        title="{} · {}".format(label, str(alert.get("rule_id") or "-")),
        detail=detail,
        severity=node_severity,
        alert_id=str(alert.get("alert_id") or ""),
        safe_point_id=refs["safe_point_id"],
        point_id=refs["point_id"],
        extra={
            "verdict": verdict,
            "rule_id": str(alert.get("rule_id") or ""),
            "command": command[:200],
            "tool": str(alert.get("tool") or ""),
            "agent": str(alert.get("agent") or ""),
        },
    )


def _node_from_event(event: Dict[str, Any]) -> Optional[TimelineNode]:
    """把一条操作事件变成一个时间线节点。

    :param event: 事件字典（``EVENT_JSON`` 契约）。
    :return: 节点；不是有效事件时返回 ``None``。
    """
    if not isinstance(event, dict):
        return None
    kind = str(event.get("kind") or "").strip()
    if not kind:
        return None
    title = str(event.get("title") or "").strip() or kind
    severity = str(event.get("severity") or SEVERITY_INFO).lower()
    if severity not in (SEVERITY_INFO, SEVERITY_WARN, SEVERITY_CRITICAL):
        severity = SEVERITY_INFO
    extra = event.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    return TimelineNode(
        ts=float(event.get("timestamp") or 0.0),
        kind=NODE_ACTION,
        title=title,
        detail=str(event.get("detail") or ""),
        severity=severity,
        safe_point_id=str(event.get("safe_point_id") or ""),
        point_id=str(event.get("point_id") or ""),
        extra=dict(extra),
    )


def _node_from_safe_point(point: Dict[str, Any]) -> Optional[TimelineNode]:
    """把一个安全点变成一个时间线节点。

    安全点是「Agent 改文件之前的那个版本」——
    它是 Diff 的基线，也是用户回看「当时是什么样子」的入口。

    :param point: 安全点字典。
    :return: 节点；不是有效安全点时返回 ``None``。
    """
    if not isinstance(point, dict):
        return None
    point_id = str(point.get("id") or "")
    if not point_id:
        return None
    label = str(point.get("label") or "").strip() or "安全点"
    return TimelineNode(
        ts=float(point.get("created_at") or 0.0),
        kind=NODE_SAFE_POINT,
        title=label,
        detail=str(point.get("workdir") or ""),
        severity=SEVERITY_INFO,
        safe_point_id=point_id,
        point_id=point_id,
        extra={
            "files": int(point.get("files_count") or 0),
            "bytes": int(point.get("bytes") or 0),
            "elapsed": float(point.get("elapsed") or 0.0),
        },
    )


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def build_timeline(
    alerts: Optional[Iterable[Dict[str, Any]]] = None,
    events: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    safe_points: Optional[Iterable[Dict[str, Any]]] = None,
    fallback_point_id: str = "",
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """把告警、事件、安全点拼成一条带时间戳的线。

    :param alerts: 告警字典列表（``ALERT_JSON`` 解析结果）。
    :param events: 事件字典列表（``EVENT_JSON`` 解析结果）。
    :param safe_points: 安全点字典列表。
    :param fallback_point_id: 告警**自己没带**安全点时的兜底基线 ID ——
        守护者缺席时红线告警没有快照，但基线总在，
        用户照样能回看「Agent 到底改了什么」。
    :param limit: 最多返回多少个节点。
    :return: 节点字典列表（按时间升序）。
    """
    nodes: List[TimelineNode] = []
    for alert in alerts or ():
        node = _node_from_alert(alert)
        if node is not None:
            if not node.point_id and fallback_point_id:
                node.point_id = str(fallback_point_id)
            nodes.append(node)
    for event in events or ():
        node = _node_from_event(event)
        if node is not None:
            nodes.append(node)
    for point in safe_points or ():
        node = _node_from_safe_point(point)
        if node is not None:
            nodes.append(node)

    nodes.sort(key=lambda item: (float(item.ts or 0.0), item.kind))

    # 相邻节点的间隔 —— 「隔了 12 分钟」比「12 分钟前」更能说明问题。
    previous = 0.0
    for node in nodes:
        gap = float(node.ts or 0.0) - previous if previous else 0.0
        node.extra["gap_text"] = format_gap(gap) if previous else ""
        previous = float(node.ts or 0.0)

    result = [node.to_dict() for node in nodes]
    max_nodes = max(int(limit or 0), 1)
    if len(result) > max_nodes:
        # 保留最新的 —— 用户关心的是「刚刚发生了什么」。
        result = result[-max_nodes:]
    return result


def summarize(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总时间线（给看板顶部用）。

    :param nodes: 节点字典列表。
    :return: ``{"counts": {…}, "first": …, "last": …, "span": …}``。
    """
    counts: Dict[str, int] = {}
    for node in nodes or ():
        kind = str(node.get("kind") or "other")
        counts[kind] = counts.get(kind, 0) + 1
    times = [float(node.get("ts") or 0.0) for node in (nodes or ())]
    times = [value for value in times if value > 0]
    first = min(times) if times else 0.0
    last = max(times) if times else 0.0
    return {
        "counts": counts,
        "total": len(nodes or []),
        "first": first,
        "last": last,
        "span": format_gap(last - first) if first and last else "",
        "first_text": format_clock(first),
        "last_text": format_clock(last),
    }


def render_timeline_text(nodes: List[Dict[str, Any]]) -> str:
    """把时间线渲染成终端文本。

    :param nodes: 节点字典列表。
    :return: 多行文本。
    """
    if not nodes:
        return "（这次会话还没有可复盘的节点。）"
    lines = ["=" * 62, "  哨兵案发时间线", "=" * 62]
    for node in nodes:
        gap = str(node.get("extra", {}).get("gap_text") or "")
        prefix = "{}（距上一步 {}）".format(node.get("time") or "--:--:--", gap) \
            if gap else str(node.get("time") or "--:--:--")
        lines.append("{}  {}".format(prefix, node.get("title") or ""))
        detail = str(node.get("detail") or "").strip()
        if detail:
            lines.append("          {}".format(detail))
    info = summarize(nodes)
    lines.append("-" * 62)
    lines.append(
        "共 {} 个节点 · {} → {} · 跨度 {}".format(
            info["total"],
            info["first_text"] or "--:--:--",
            info["last_text"] or "--:--:--",
            info["span"] or "-",
        )
    )
    lines.append("=" * 62)
    return "\n".join(lines)
