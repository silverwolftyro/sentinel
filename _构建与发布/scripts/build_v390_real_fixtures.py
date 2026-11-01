#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从**真实推理日志**里提取 v3.9.0 回归夹具。

素材是哪来的
============

``tests/fixtures/v390_raw_inference_log.jsonl``
（源文件：仓库根目录的 ``2026-10-31.txt``，1,262,856 字节 / 1.2 MB）
是哨兵审计器（client ``sentinel-auditor/0.6``）打给本地 NInfer
的**真实推理记录**，一行一条 JSON，共 61 条：

* ``request.raw_body`` —— 完整的 system prompt + user prompt；
* ``response.content`` —— 模型的**真实判定**。

其中 41 条是 L2/L3 的**意图漂移判定**（system prompt 是漂移判定模板），
26 条判 ``drift=true``、15 条判 ``drift=false``。

本脚本挑哪几条
==============

只挑**含 CHANGELOG 的漂移判定窗口**，而且按**判定结果**分成两类：

* ``false_positive`` —— 模型判了 ``drift=true``，但步骤内容恰恰是
  「版本 bump + CHANGELOG + 版本文档」这种**发版前的常规铺垫**
  → 这就是用户反馈的误报，v3.9.0 必须让它不再触发；
* ``control`` —— 含 CHANGELOG 但模型判 ``drift=false``
  → 作为对照组，v3.9.0 修完**必须仍然是** ``drift=false``。

为什么反解回事件
================

日志里存的是**生产滑动窗口的摘要**（``第N步 [思考] …``），
不是原始事件。生产白名单读的是**原始事件**，所以这里把摘要
按 ``build_window_summary`` 的输出格式**反解**回事件：

* ``第N步 [思考] 文本`` → ``{"type": "thinking", "text": 文本}``
* ``第N步 [工具名] 剩余`` → ``{"type": "tool_call", "tool": {...}}``

⚠️ 摘要里的思考文本**已按生产的 ``SUMMARY_STEP_LIMIT`` 截断**，
这一点在夹具里如实标注（``note`` 字段）。

用法
====

.. code-block:: bash

    python _构建与发布/scripts/build_v390_real_fixtures.py

产物：``tests/fixtures/v390_real_drift_falsepositives.json``
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 原始推理日志的**候选位置**（按顺序找第一个存在的）。
#:
#: 默认放仓库根目录 —— 它是 1.2 MB 的原始素材，不适合进版本库。
#: 夹具本身是**自包含**的（基线 prompt、目标、窗口、原始判定全在里面），
#: 所以这个脚本只是「重新生成夹具」的工具，不是夹具的前置依赖。
RAW_LOG_CANDIDATES: Tuple[str, ...] = (
    os.path.join(_ROOT, "2026-10-31.txt"),
    os.path.join(
        _ROOT, "tests", "fixtures", "v390_raw_inference_log.jsonl"
    ),
    os.path.join(_ROOT, "logs", "v390_raw_inference_log.jsonl"),
)

#: 夹具落盘位置。
FIXTURE_PATH = os.path.join(
    _ROOT, "tests", "fixtures", "v390_real_drift_falsepositives.json"
)

#: 要挑几个「误报」用例 + 几个「对照」用例。
WANTED_FALSE_POSITIVE = 2
WANTED_CONTROL = 1

#: 漂移判定 system prompt 的特征句。
_DRIFT_MARKER = "偏离了用户最初交给它的目标"

#: 窗口摘要里的一行：``第3步 [思考] 文本``。
_STEP_PATTERN = re.compile(r"^第(\d+)步\s+\[([^\]]+)\]\s*(.*)$")

#: 工具步骤里的 ``key=value`` 参数。
_ARG_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _resolve_raw_log(explicit: Optional[str]) -> str:
    """定位原始推理日志。

    :param explicit: 命令行显式给的路径（可为 ``None``）。
    :return: 日志绝对路径。
    :raises FileNotFoundError: 一个候选都没找到时。
    """
    if explicit:
        path = os.path.abspath(explicit)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"指定的原始日志不存在：{path}")
        return path
    for candidate in RAW_LOG_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(
        "找不到原始推理日志。已尝试：\n  "
        + "\n  ".join(RAW_LOG_CANDIDATES)
        + "\n用 --raw-log <路径> 显式指定。"
    )


def _file_facts(path: str) -> Dict[str, Any]:
    """算原始日志的指纹（大小 + SHA-256）。

    :param path: 日志路径。
    :return: 指纹字典。
    """
    digest = hashlib.sha256()
    size = 0
    with io.open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _read_records(path: str) -> List[Dict[str, Any]]:
    """读原始日志，返回逐条记录。"""
    records: List[Dict[str, Any]] = []
    with io.open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            records.append({"line": line_no, "record": json.loads(stripped)})
    return records


def _split_prompt(user: str) -> Tuple[str, str]:
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


def _reconstruct_events(steps_text: str) -> List[Dict[str, Any]]:
    """把窗口摘要**反解**回原始事件。

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
        tool: Dict[str, Any] = {"name": kind}
        arguments: Dict[str, Any] = {}
        arg_match = _ARG_PATTERN.match(rest.strip())
        if arg_match:
            arguments[arg_match.group(1)] = arg_match.group(2)
        else:
            tool["command"] = rest
        if arguments:
            tool["arguments"] = arguments
        events.append({"type": "tool_call", "tool": tool})
    return events


def _is_drift_eval(system: str) -> bool:
    """判断这条记录是不是 L2/L3 的漂移判定。"""
    return _DRIFT_MARKER in system[:400]


def _mentions_changelog(text: str) -> bool:
    """窗口文本里是否提到 CHANGELOG。"""
    return "changelog" in text.lower()


def _pick_cases(
    records: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按规则挑出「误报」与「对照」两类用例。

    :param records: 原始记录列表。
    :return: ``(误报用例, 对照用例)``。
    """
    false_positive: List[Dict[str, Any]] = []
    control: List[Dict[str, Any]] = []
    for item in records:
        record = item["record"]
        request = record.get("request") or {}
        try:
            body = json.loads(request.get("raw_body") or "{}")
        except (TypeError, ValueError):
            continue
        messages = body.get("messages") or []
        system = ""
        user = ""
        for message in messages:
            content = message.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if message.get("role") == "system":
                system = content
            elif message.get("role") == "user":
                user = content
        if not _is_drift_eval(system):
            continue
        goal, steps_text = _split_prompt(user)
        if not steps_text or not _mentions_changelog(steps_text):
            continue
        try:
            verdict = json.loads(str((record.get("response") or {}).get("content")))
        except (TypeError, ValueError):
            continue
        if not isinstance(verdict, dict) or "drift" not in verdict:
            continue
        payload = {
            "line": item["line"],
            "record": record,
            "system": system,
            "user": user,
            "goal": goal,
            "steps_text": steps_text,
            "verdict": verdict,
        }
        if verdict.get("drift") is True and len(false_positive) < WANTED_FALSE_POSITIVE:
            false_positive.append(payload)
        elif verdict.get("drift") is False and len(control) < WANTED_CONTROL:
            control.append(payload)
        if (
            len(false_positive) >= WANTED_FALSE_POSITIVE
            and len(control) >= WANTED_CONTROL
        ):
            break
    return false_positive, control


def _build_case(
    payload: Dict[str, Any],
    case_id: str,
    kind: str,
    source_name: str,
) -> Dict[str, Any]:
    """把一条原始记录加工成夹具用例。

    :param payload: :func:`_pick_cases` 挑出的载荷。
    :param case_id: 用例编号。
    :param kind: ``false_positive`` 或 ``control``。
    :param source_name: 原始日志的文件名（写进出处信息）。
    :return: 夹具用例字典。
    """
    record = payload["record"]
    request = record.get("request") or {}
    steps_text = payload["steps_text"]
    return {
        "case_id": case_id,
        "kind": kind,
        "source": {
            "file": source_name,
            "line_number": payload["line"],
            "timestamp_unix_ms": record.get("timestamp_unix_ms"),
            "ts_text": record.get("ts_text"),
            "elapsed_seconds": record.get("elapsed"),
            "model": request.get("model"),
            "client_id": (record.get("client") or {}).get("client_id"),
            "max_tokens": request.get("max_tokens"),
        },
        "baseline_system_prompt": payload["system"],
        "goal": payload["goal"],
        "window_steps_text": steps_text,
        "window_events": _reconstruct_events(steps_text),
        "baseline_verdict": payload["verdict"],
        "expect": {
            "drift": False,
            "bypassed": True,
            "reason_contains": "常规演进文件：changelog",
        },
    }


def build_fixture(raw_log_path: str) -> Dict[str, Any]:
    """造出夹具字典（不落盘）。

    :param raw_log_path: 原始推理日志路径。
    :return: 夹具字典。
    """
    records = _read_records(raw_log_path)
    facts = _file_facts(raw_log_path)
    false_positive, control = _pick_cases(records)
    if len(false_positive) < WANTED_FALSE_POSITIVE:
        raise RuntimeError(
            f"只挑到 {len(false_positive)} 个误报用例，"
            f"少于要求的 {WANTED_FALSE_POSITIVE} 个"
        )
    if len(control) < WANTED_CONTROL:
        raise RuntimeError(
            f"只挑到 {len(control)} 个对照用例，"
            f"少于要求的 {WANTED_CONTROL} 个"
        )

    cases: List[Dict[str, Any]] = []
    source_name = os.path.basename(raw_log_path)
    for index, payload in enumerate(false_positive, start=1):
        cases.append(
            _build_case(
                payload,
                f"REAL-FP-{index:02d}",
                "false_positive",
                source_name,
            )
        )
    for index, payload in enumerate(control, start=1):
        cases.append(
            _build_case(
                payload,
                f"REAL-CTRL-{index:02d}",
                "control",
                source_name,
            )
        )

    return {
        "generated_by": (
            "_构建与发布/scripts/build_v390_real_fixtures.py"
        ),
        "source_log": {
            "file": os.path.basename(raw_log_path),
            "origin": (
                "哨兵审计器打给本地 NInfer 的真实推理记录 —— "
                "仓库根目录的 2026-10-31.txt（1,262,856 字节 / 1.2 MB）"
            ),
            "bytes": facts["bytes"],
            "sha256": facts["sha256"],
            "records_total": len(records),
            "client_id": "sentinel-auditor/0.6",
            "description": (
                "哨兵审计器打给本地 NInfer 的真实推理记录，"
                "一行一条 JSON，含完整 request prompt 与真实 response。"
            ),
        },
        "note": (
            "本夹具的每一行都来自**真实生产推理**，不是构造出来的。"
            "误报用例的 baseline_verdict 是当时模型给出的 drift=true；"
            "对照组是当时模型给出的 drift=false —— "
            "v3.9.0 修完之后，两类都必须变成 drift=false，"
            "且不再消耗一次 L2 推理。"
            "⚠️ window_events 是由日志里的「最近步骤」摘要"
            "**反解**回来的，思考文本已按生产的 SUMMARY_STEP_LIMIT 截断。"
        ),
        "cases": cases,
    }


def _digest(payload: Any) -> str:
    """算稳定的内容哈希。"""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    """命令行入口。

    :return: 进程退出码（0 = 成功）。
    """
    parser = argparse.ArgumentParser(
        description="从真实推理日志里提取 v3.9.0 回归夹具"
    )
    parser.add_argument(
        "--raw-log",
        default=None,
        help="原始推理日志路径（默认按内置候选顺序查找）",
    )
    args = parser.parse_args()
    try:
        raw_log_path = _resolve_raw_log(args.raw_log)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"原始日志：{raw_log_path}")
    fixture = build_fixture(raw_log_path)
    parent = os.path.dirname(FIXTURE_PATH)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with io.open(FIXTURE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(fixture, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"夹具已落盘：{FIXTURE_PATH}")
    print(f"用例数：{len(fixture['cases'])}")
    print(f"内容哈希：{_digest(fixture['cases'])}")
    for row in fixture["cases"]:
        print(
            f"  {row['case_id']} [{row['kind']}] "
            f"<- 日志第 {row['source']['line_number']} 行 "
            f"(baseline drift={row['baseline_verdict'].get('drift')})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
