# -*- coding: utf-8 -*-
"""从真实 LLM 请求日志生成 v4.0.1 A/B 评测夹具。

做什么
======

把哨兵在真实生产里发出去的 **41 个漂移判定窗口** 从原始请求日志里
抽出来，反解成**事件序列**，再拼上**人工裁决的真值**，落成一份
可复现的 JSONL 夹具。

为什么要有这一步
================

A/B 评测要保证「只换摘要格式」这件事成立，就必须能**同时**造出
两臂的输入：

* A 臂 = ``build_window_summary(events)``（旧格式）；
* B 臂 = ``build_compressed_summary(events, goal)``（v4.0.0 新格式）。

事件序列是唯一的信息源 —— 只有它能同时喂出两种摘要。

保真度校验
==========

反解出来的事件**必须**能逐字节还原日志里当初那一份 A 臂摘要。
本脚本会逐条比对，比对结果写进统计里：

* ``fidelity_matched`` —— 重构出的 A 臂摘要与日志原文**逐字相同**；
* ``fidelity_mismatched`` —— 不同（这类窗口的 A 臂输入不可信）。

⚠️ 原始日志 ``2026-10-31.txt`` **不入库**（含真实请求/响应与
客户端信息）；入库的是本脚本产出的**派生夹具**。

用法
====

.. code-block:: text

    python _构建与发布/scripts/build_v401_drift_fixtures.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = str(Path(__file__).resolve().parents[2])
"""仓库根目录。

⚠️ v4.3.2：这里原来硬编码成 ``r"D:\\项目\\<项目目录>"`` ——
那是**作者本机**的路径，换一台机器就跑不了，而且会把本地目录布局
带进公开仓库。改成从本文件位置推导，哪儿 clone 都能跑。
"""
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.engine import (  # noqa: E402 —— 需先补 sys.path
    build_window_summary,
    detect_routine_maintenance,
)

#: 漂移判定请求的 system prompt 特征串。
_DRIFT_MARKER = "偏离了用户最初交给它的目标"

#: 旧格式摘要的单步前缀。
_STEP_PATTERN = re.compile(r"^第(\d+)步\s+\[([^\]]+)\]\s*(.*)$")

#: ``key=value`` 形式的工具参数摘要。
_ARG_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

#: 原始请求日志（不入库）。
DEFAULT_LOG = os.path.join(ROOT, "2026-10-31.txt")

#: 人工裁决真值表。
TRUTH_PATH = os.path.join(
    ROOT, "_构建与发布", "scripts", "v401_drift_truth.json"
)

#: 产出的夹具。
FIXTURE_PATH = os.path.join(
    ROOT, "tests", "fixtures", "v401_drift_windows.jsonl"
)


def load_records(path: str) -> List[Dict[str, Any]]:
    """读取原始请求日志。

    :param path: 日志路径。
    :return: ``{"line": 行号, "record": 记录}`` 列表。
    """
    records: List[Dict[str, Any]] = []
    with io.open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            records.append({"line": line_no, "record": json.loads(stripped)})
    return records


def split_prompt(user: str) -> Tuple[str, str]:
    """把 user prompt 拆成 ``(原始目标, 最近步骤)``。

    :param user: user prompt 全文。
    :return: 二元组；任一段没找到时对应位置为空串。
    """
    goal = ""
    steps = ""
    goal_match = re.search(
        r"【原始目标】\n(.*?)(?=\n【最近步骤】)", user, re.DOTALL
    )
    if goal_match:
        goal = goal_match.group(1).strip()
    steps_match = re.search(
        r"【最近步骤】\n(.*?)(?=\n现在输出 JSON。)", user, re.DOTALL
    )
    if steps_match:
        steps = steps_match.group(1).strip()
    return goal, steps


def reconstruct_events(steps_text: str) -> List[Dict[str, Any]]:
    """把窗口摘要**反解**回原始事件。

    ⚠️ 工具参数**同时**写进 ``tool["command"]`` 与
    ``tool["arguments"]`` —— 前者是旧摘要格式要读的，后者是
    v4.0.0 压缩器要读的（压缩器按参数键族取目标，
    只认 ``command`` 键读不到）。两边都写，A 臂才能逐字还原。

    :param steps_text: ``第N步 [思考] …`` 形式的摘要正文。
    :return: 事件列表（生产 ``IntentTracker`` 认识的结构）。
    """
    events: List[Dict[str, Any]] = []
    for raw_line in steps_text.splitlines():
        match = _STEP_PATTERN.match(raw_line.strip())
        if not match:
            continue
        _index, kind, rest = match.groups()
        if kind == "思考":
            events.append({"type": "thinking", "text": rest})
            continue
        tool: Dict[str, Any] = {"name": kind, "command": rest}
        arguments: Dict[str, Any] = {}
        arg_match = _ARG_PATTERN.match(rest.strip())
        if arg_match:
            arguments[arg_match.group(1)] = arg_match.group(2)
        else:
            arguments["command"] = rest
        tool["arguments"] = arguments
        events.append({"type": "tool_call", "tool": tool})
    return events


def extract_drift_windows(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从日志里挑出全部漂移判定窗口。

    :param records: :func:`load_records` 的结果。
    :return: 每个窗口一个字典。
    """
    windows: List[Dict[str, Any]] = []
    goal_ids: Dict[str, int] = {}
    for item in records:
        record = item["record"]
        request = record.get("request") or {}
        try:
            body = json.loads(request.get("raw_body") or "{}")
        except (TypeError, ValueError):
            continue
        system = ""
        user = ""
        for message in body.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if message.get("role") == "system":
                system = content
            elif message.get("role") == "user":
                user = content
        if _DRIFT_MARKER not in system[:400]:
            continue
        goal, steps = split_prompt(user)
        if not steps:
            continue
        try:
            verdict = json.loads(str((record.get("response") or {}).get("content")))
        except (TypeError, ValueError):
            continue
        if not isinstance(verdict, dict) or "drift" not in verdict:
            continue
        goal_id = goal_ids.setdefault(goal, len(goal_ids) + 1)
        windows.append(
            {
                "line": item["line"],
                "goal_id": goal_id,
                "goal": goal,
                "summary_a": steps,
                "historical_drift": bool(verdict.get("drift")),
                "historical_reason": str(verdict.get("reason") or ""),
            }
        )
    return windows


def load_truth() -> Dict[str, Any]:
    """读取人工裁决真值表。

    :return: 真值表字典。
    """
    with io.open(TRUTH_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_fixture() -> Dict[str, Any]:
    """生成夹具。

    :return: 统计信息字典。
    """
    truth = load_truth()
    truth_map = {int(row["id"]): row for row in truth["windows"]}
    windows = extract_drift_windows(load_records(DEFAULT_LOG))

    rows: List[Dict[str, Any]] = []
    matched = 0
    mismatched = 0
    missing_truth: List[int] = []
    for index, window in enumerate(windows, start=1):
        events = reconstruct_events(window["summary_a"])
        rebuilt = build_window_summary(events)
        if rebuilt == window["summary_a"]:
            matched += 1
        else:
            mismatched += 1
        row = truth_map.get(index)
        if row is None:
            missing_truth.append(index)
            continue
        bypass = detect_routine_maintenance(events)
        rows.append(
            {
                "id": index,
                "line": window["line"],
                "goal_id": window["goal_id"],
                "goal": window["goal"],
                "events": events,
                "summary_a": window["summary_a"],
                "fidelity_ok": rebuilt == window["summary_a"],
                "historical_drift": window["historical_drift"],
                "historical_reason": window["historical_reason"],
                "truth_drift": bool(row["truth_drift"]),
                "category": str(row["category"]),
                "rationale": str(row["rationale"]),
                "whitelist_hit": bypass,
            }
        )

    os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)
    with io.open(FIXTURE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )

    return {
        "windows": len(windows),
        "rows": len(rows),
        "fidelity_matched": matched,
        "fidelity_mismatched": mismatched,
        "missing_truth": missing_truth,
        "fixture": FIXTURE_PATH,
        "bytes": os.path.getsize(FIXTURE_PATH),
    }


def main() -> int:
    """主流程。

    :return: 进程退出码。
    """
    stats = build_fixture()
    print("windows            =", stats["windows"])
    print("rows               =", stats["rows"])
    print("fidelity_matched   =", stats["fidelity_matched"])
    print("fidelity_mismatched=", stats["fidelity_mismatched"])
    print("missing_truth      =", stats["missing_truth"])
    print("fixture            =", stats["fixture"])
    print("bytes              =", stats["bytes"])
    return 0 if not stats["missing_truth"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
