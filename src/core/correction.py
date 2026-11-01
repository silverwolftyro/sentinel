"""一键复制纠偏提示 —— 把告警变成「能直接粘给 Agent 的话」。

为什么要有它
============

光看告警**不够**。用户半夜看到一条 ``[STUCK]``，真正要回答的问题是：

> 我该跟 Agent 说点什么，让它从死循环里出来？

这个「说点什么」不该由用户临场想 —— 上下文都在哨兵手里
（人话解释、触发规则、原始命令、最近几步在干什么、任务目标），
拼成一段**能直接粘贴**的 Prompt 是最省事的。

两种提示的区别
==============

========================= ==========================================
按钮                       用途
========================= ==========================================
【复制纠偏提示】            非红线告警（``[STUCK]`` / ``LOOP`` /
                          意图漂移 / 一般危险命令）——
                          告诉 Agent「你跑偏了，回到任务目标」
【复制拦截原因】            **红线级**拦截 ——
                          告诉 Agent「这一步不可逆，换个做法」
========================= ==========================================

⚠️ 提示里**不放任何密钥**：送进模型前哨兵已经脱敏过一遍，
这里再按原样引用，等于把脱敏白做了。
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = [
    "COPY_LABEL_CORRECTION",
    "COPY_LABEL_INTERCEPT",
    "SEVERITY_RED_LINE",
    "build_correction_prompt",
    "build_intercept_prompt",
]

#: 红线级的 severity 取值。
SEVERITY_RED_LINE = "red_line"

#: 普通纠偏提示的按钮文案。
COPY_LABEL_CORRECTION = "复制纠偏提示"

#: 红线拦截提示的按钮文案。
COPY_LABEL_INTERCEPT = "复制拦截原因"

#: 最近操作记录最多列几条（再多就看不清重点了）。
MAX_HISTORY_ITEMS = 6

#: 单条命令最多写多少字符。
MAX_COMMAND_CHARS = 400


def copy_label(alert: Dict[str, Any]) -> str:
    """这个告警该用哪个按钮文案。

    :param alert: 告警字典。
    :return: ``【复制拦截原因】`` 或 ``【复制纠偏提示】``。
    """
    if is_red_line(alert):
        return COPY_LABEL_INTERCEPT
    return COPY_LABEL_CORRECTION


def is_red_line(alert: Dict[str, Any]) -> bool:
    """判断是不是红线级告警。

    :param alert: 告警字典。
    :return: 是红线级返回 ``True``。
    """
    data = alert if isinstance(alert, dict) else {}
    return str(data.get("severity") or "") == SEVERITY_RED_LINE


def build_correction_prompt(
    alert: Dict[str, Any],
    *,
    task_goal: str = "",
) -> str:
    """生成「纠偏提示」——给非红线告警用。

    :param alert:    告警字典（要含 ``explanation`` / ``command`` 等）。
    :param task_goal: 任务目标（意图锚点）。
    :return: 可直接粘贴给 Agent 的 Prompt 文本。
    """
    data = alert if isinstance(alert, dict) else {}
    lines: List[str] = []

    verdict = str(data.get("verdict") or "UNKNOWN")
    rule_id = str(data.get("rule_id") or "-")
    reason = str(data.get("reason") or "")

    lines.append(f"【哨兵纠偏提示 · {verdict}】")
    lines.append("")
    lines.append("哨兵在监控你的运行过程。刚才这一步可能已经偏离了原始任务，")
    lines.append("请先停下来确认方向，再继续。")
    lines.append("")
    lines.append("## 触发情况")
    lines.append("")
    lines.append(f"- 判定：`{verdict}`（规则 `{rule_id}`）")
    if reason:
        lines.append(f"- 哨兵判定原因：{reason}")
    evidence = str(data.get("evidence") or "")
    if evidence:
        lines.append(f"- 证据：{_clip(evidence)}")
    command = str(data.get("command") or "")
    if command:
        lines.append(f"- 相关操作：`{_clip(command)}`")
    goal = str(task_goal or "")
    if goal:
        lines.append(f"- 原始任务目标：{goal}")
    lines.append("")

    explanation = str(data.get("explanation") or "")
    if explanation:
        lines.append("## 哨兵的解读")
        lines.append("")
        lines.append(explanation.strip())
        lines.append("")

    history = _history_lines(data)
    if history:
        lines.append("## 最近几步在干什么")
        lines.append("")
        lines.extend(history)
        lines.append("")

    lines.append("## 请这样做")
    lines.append("")
    lines.append("1. 先用一句话复述你当前认为的任务目标，看看它和上面的目标是否一致。")
    lines.append("2. 如果已经跑偏：直接说明你接下来要做什么、为什么它服务于这个目标，")
    lines.append("   然后**换一个思路** —— 不要重复刚才那一步。")
    lines.append("3. 如果卡在同一个错误上：不要重试同一条命令，")
    lines.append("   先说明你从失败里学到了什么，再换一种做法。")
    lines.append("4. 如果你确实需要做危险操作：先讲清楚影响范围和可逆方案，")
    lines.append("   确认后再执行。")
    lines.append("")
    return "\n".join(lines)


def build_intercept_prompt(
    alert: Dict[str, Any],
    *,
    task_goal: str = "",
) -> str:
    """生成「拦截原因」——给**红线级**告警用。

    :param alert:    告警字典。
    :param task_goal: 任务目标（意图锚点）。
    :return: 可直接粘贴给 Agent 的 Prompt 文本。
    """
    data = alert if isinstance(alert, dict) else {}
    lines: List[str] = []

    rule_id = str(data.get("rule_id") or "-")
    reason = str(data.get("reason") or "")
    command = str(data.get("command") or "")

    lines.append("【哨兵拦截报告 · 红线级】")
    lines.append("")
    lines.append("你上一步的操作被哨兵判定为**不可逆的高危操作**，")
    lines.append("已经被暂停 —— 它没有被执行，你需要换一种做法。")
    lines.append("")
    lines.append("## 触发情况")
    lines.append("")
    lines.append(f"- 触发规则：`{rule_id}`")
    if reason:
        lines.append(f"- 判定原因：{reason}")
    if command:
        lines.append("- 被拦截的命令：")
        lines.append("")
        lines.append("```")
        lines.append(_clip(command))
        lines.append("```")
    goal = str(task_goal or "")
    if goal:
        lines.append(f"- 原始任务目标：{goal}")
    lines.append("")

    explanation = str(data.get("explanation") or "")
    if explanation:
        lines.append("## 哨兵的解读")
        lines.append("")
        lines.append(explanation.strip())
        lines.append("")

    lines.append("## 请这样做")
    lines.append("")
    lines.append("1. **不要重试刚才那条命令。**")
    lines.append("2. 先说明你这一步想达成什么，以及它为什么必须这么做。")
    lines.append("3. 换一个**可逆**的做法：先 dry-run / 先备份 / 缩小范围到最小，")
    lines.append("   或者改用不会造成不可逆后果的替代方案。")
    lines.append("4. 如果这个操作确实不可替代，请明确告诉用户需要人工确认，")
    lines.append("   并说明影响范围。")
    lines.append("")
    return "\n".join(lines)


def build_prompt(
    alert: Dict[str, Any],
    *,
    task_goal: str = "",
) -> str:
    """按告警类型挑一个生成器。

    :param alert:    告警字典。
    :param task_goal: 任务目标（意图锚点）。
    :return: Prompt 文本。
    """
    if is_red_line(alert):
        return build_intercept_prompt(alert, task_goal=task_goal)
    return build_correction_prompt(alert, task_goal=task_goal)


def _history_lines(alert: Dict[str, Any]) -> List[str]:
    """把 ``context_history`` 渲染成「最近几步在干什么」。

    :param alert: 告警字典。
    :return: 行列表；没有历史时返回空列表。
    """
    history = alert.get("context_history")
    if not isinstance(history, list):
        return []
    lines: List[str] = []
    for index, item in enumerate(history[-MAX_HISTORY_ITEMS:], start=1):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "?")
        command = str(item.get("command") or item.get("target") or "")
        failed = bool(item.get("is_error"))
        mark = "（失败）" if failed else ""
        if command:
            lines.append(f"{index}. {kind}{mark}：`{_clip(command)}`")
        else:
            lines.append(f"{index}. {kind}{mark}")
    return lines


def _clip(text: str, limit: int = MAX_COMMAND_CHARS) -> str:
    """把过长的文本截断（避免提示变得没法读）。

    :param text:  原文。
    :param limit: 最大长度。
    :return: 截断后的文本。
    """
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"…（已截断，共 {len(raw)} 字）"
