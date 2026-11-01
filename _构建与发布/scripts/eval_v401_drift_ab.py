# -*- coding: utf-8 -*-
"""v4.0.1 任务二：漂移判定 A/B 评测（旧摘要 vs 压缩摘要）。

评什么
======

同一个模型、同一份 System Prompt、同一个目标、同一个温度与
``max_tokens`` —— **只换 user prompt 里【最近步骤】那一段的生成方式**：

* **A 臂** = ``build_window_summary(events)``（v3.9.0 之前的旧格式）；
* **B 臂** = ``build_compressed_summary(events, goal)``（v4.0.0 新格式）。

这样测的才是「**格式**」，不是「Prompt」。

怎么保证公平
============

1. **走生产同一条链路** —— 直接用 ``build_routing_provider()`` 造出来的
   L2 客户端调 ``evaluate_drift()``：同一个 system prompt、同一个
   脱敏、同一个 ``strip_volatile``、同一个输出解析器；
2. **逐条记录实际发出的 prompt**，并做**字符串手术校验** ——
   把 A 臂 prompt 里的【最近步骤】段换成 B 臂的那一段之后，
   必须与 B 臂 prompt **逐字节相同**。不等就说明「只换了一段」
   这件事不成立，评测作废；
3. **两臂调用顺序逐窗交替**（偶数窗 A 先、奇数窗 B 先），
   避免「谁先跑谁占便宜」的顺序偏差。

产物
====

* ``logs/v401-demo/drift_ab_results.jsonl`` —— 逐次调用明细（可断点续跑）；
* ``logs/v401-demo/drift_ab_metrics.json`` —— 指标；
* ``logs/v401-demo/drift_ab_report.md`` —— 人读的报告。

用法
====

.. code-block:: text

    python _构建与发布/scripts/eval_v401_drift_ab.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = str(Path(__file__).resolve().parents[2])
"""仓库根目录。

⚠️ v4.3.2：这里原来硬编码成 ``r"D:\\项目\\<项目目录>"`` ——
那是**作者本机**的路径，换一台机器就跑不了，而且会把本地目录布局
带进公开仓库。改成从本文件位置推导，哪儿 clone 都能跑。
"""
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config_loader import load_config  # noqa: E402 —— 需先补 sys.path
from src.core.engine import (  # noqa: E402 —— 需先补 sys.path
    build_compressed_summary,
    build_window_summary,
)
from src.llm.factory import build_routing_provider  # noqa: E402

#: 夹具路径。
FIXTURE_PATH = os.path.join(
    ROOT, "tests", "fixtures", "v401_drift_windows.jsonl"
)

#: 产物目录。
OUT_DIR = os.path.join(ROOT, "logs", "v401-demo")

#: 逐次调用明细。
RESULTS_PATH = os.path.join(OUT_DIR, "drift_ab_results.jsonl")

#: 指标。
METRICS_PATH = os.path.join(OUT_DIR, "drift_ab_metrics.json")

#: 报告。
REPORT_PATH = os.path.join(OUT_DIR, "drift_ab_report.md")

#: 两臂的名字。
ARMS = ("A", "B")


def load_fixture() -> List[Dict[str, Any]]:
    """读取夹具。

    :return: 窗口列表。
    """
    rows: List[Dict[str, Any]] = []
    with io.open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def load_done() -> Dict[Tuple[int, str], Dict[str, Any]]:
    """读取已完成的调用（断点续跑）。

    :return: ``(窗口 id, 臂名) -> 结果`` 字典。
    """
    done: Dict[Tuple[int, str], Dict[str, Any]] = {}
    if not os.path.exists(RESULTS_PATH):
        return done
    with io.open(RESULTS_PATH, "r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except ValueError:
                continue
            done[(int(row["id"]), str(row["arm"]))] = row
    return done


def load_prompts() -> Dict[Tuple[int, str], Tuple[str, str]]:
    """读取**已落盘**的 prompt 记录（支持断点续跑）。

    prompt 手术校验必须拿**真的发出去过的** prompt，
    所以每次调用都把 system / user 一起写进结果文件；
    续跑时从这里把「没重跑的那些窗口」的 prompt 捞回来。

    :return: ``(窗口 id, 臂名) -> (system, user)`` 字典。
    """
    out: Dict[Tuple[int, str], Tuple[str, str]] = {}
    if not os.path.exists(RESULTS_PATH):
        return out
    with io.open(RESULTS_PATH, "r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except ValueError:
                continue
            user = row.get("prompt_user")
            if not isinstance(user, str):
                continue
            system = row.get("prompt_system")
            out[(int(row["id"]), str(row["arm"]))] = (
                str(system) if isinstance(system, str) else "",
                user,
            )
    return out


def arm_summary(row: Dict[str, Any], arm: str) -> str:
    """算某一臂的【最近步骤】正文。

    :param row: 夹具行。
    :param arm: ``"A"`` 或 ``"B"``。
    :return: 摘要正文。
    """
    if arm == "A":
        return build_window_summary(row["events"])
    return build_compressed_summary(row["events"], row["goal"])


def make_provider():
    """造一个**生产同款**的 L2 客户端，并挂上 prompt 记录钩子。

    :return: ``(provider, captured)``；``captured`` 是每次实际发出的
        ``(system, user)`` 列表。
    """
    config = load_config()
    provider = build_routing_provider(config)
    captured: List[Tuple[str, str]] = []
    original_chat = provider.chat

    def spy(system: str, user: str) -> str:
        captured.append((str(system), str(user)))
        return original_chat(system, user)

    provider.chat = spy  # type: ignore[method-assign]
    return provider, captured


def run_arm(
    provider: Any,
    row: Dict[str, Any],
    arm: str,
) -> Dict[str, Any]:
    """跑一臂。

    :param provider: L2 客户端。
    :param row: 夹具行。
    :param arm: ``"A"`` 或 ``"B"``。
    :return: 结果字典。
    """
    summary = arm_summary(row, arm)
    started = time.monotonic()
    try:
        result = provider.evaluate_drift(row["goal"], summary)
    except Exception as exc:  # noqa: BLE001 —— 单次失败不该中断整轮
        return {
            "id": row["id"],
            "arm": arm,
            "ok": False,
            "drift": None,
            "reason": "",
            "degraded": True,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "summary_len": len(summary),
        }
    return {
        "id": row["id"],
        "arm": arm,
        "ok": not result.degraded,
        "drift": bool(result.drift),
        "reason": str(result.reason or ""),
        "degraded": bool(result.degraded),
        "error": "" if not result.degraded else str(result.detail or ""),
        "latency_ms": int(result.latency_ms),
        "summary_len": len(summary),
    }


def _predict(results: Dict[Tuple[int, str], Dict[str, Any]],
             arm: str) -> Dict[int, bool]:
    """取某一臂的预测。

    :param results: 全部结果。
    :param arm: 臂名。
    :return: ``窗口 id -> drift`` 字典。
    """
    out: Dict[int, bool] = {}
    for (window_id, item_arm), row in results.items():
        if item_arm != arm or not row.get("ok"):
            continue
        out[window_id] = bool(row.get("drift"))
    return out


def metrics(
    rows: List[Dict[str, Any]],
    truth: Dict[int, bool],
    predictions: Dict[int, bool],
) -> Dict[str, Any]:
    """算一组预测的指标。

    :param rows: 夹具行。
    :param truth: ``窗口 id -> 真值 drift``。
    :param predictions: ``窗口 id -> 预测 drift``。
    :return: 指标字典。
    """
    tp = fp = tn = fn = 0
    for row in rows:
        window_id = int(row["id"])
        if window_id not in truth or window_id not in predictions:
            continue
        actual = truth[window_id]
        guess = predictions[window_id]
        if actual and guess:
            tp += 1
        elif actual and not guess:
            fn += 1
        elif not actual and guess:
            fp += 1
        else:
            tn += 1
    total = tp + fp + tn + fn
    routine = fp + tn
    drift = tp + fn
    return {
        "n": total,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy": round((tp + tn) / total, 4) if total else None,
        "false_positive_rate": round(fp / routine, 4) if routine else None,
        "false_negative_rate": round(fn / drift, 4) if drift else None,
        "routine_windows": routine,
        "drift_windows": drift,
    }


def _fmt(value: Optional[float]) -> str:
    """把比例格式化成百分数。

    :param value: 比例；``None`` 表示没数据。
    :return: 百分数字符串。
    """
    if value is None:
        return "—"
    return "{:.1f}%".format(value * 100)


def write_report(
    rows: List[Dict[str, Any]],
    results: Dict[Tuple[int, str], Dict[str, Any]],
    truth: Dict[int, bool],
    alt_truth: Dict[int, bool],
    provider_info: Dict[str, Any],
    audit: Dict[str, Any],
) -> None:
    """写人读的评测报告。

    :param rows: 夹具行。
    :param results: 全部结果。
    :param truth: 主口径真值。
    :param alt_truth: 会话口径真值。
    :param provider_info: 客户端信息。
    :param audit: prompt 手术校验结果。
    """
    pred_a = _predict(results, "A")
    pred_b = _predict(results, "B")
    metrics_a = metrics(rows, truth, pred_a)
    metrics_b = metrics(rows, truth, pred_b)
    alt_a = metrics(rows, alt_truth, pred_a)
    alt_b = metrics(rows, alt_truth, pred_b)
    hist = {
        int(row["id"]): bool(row["historical_drift"]) for row in rows
    }
    metrics_hist = metrics(rows, truth, hist)

    lines: List[str] = []
    lines.append("# v4.0.1 · 漂移判定 A/B 评测报告（旧摘要 vs 压缩摘要）")
    lines.append("")
    lines.append("## 1. 结论")
    lines.append("")
    lines.append(
        "- A 臂（旧格式 `build_window_summary`）：准确率 **{}**，"
        "误报率 **{}**，漏报率 **{}**。".format(
            _fmt(metrics_a["accuracy"]),
            _fmt(metrics_a["false_positive_rate"]),
            _fmt(metrics_a["false_negative_rate"]),
        )
    )
    lines.append(
        "- B 臂（压缩格式 `build_compressed_summary`）：准确率 **{}**，"
        "误报率 **{}**，漏报率 **{}**。".format(
            _fmt(metrics_b["accuracy"]),
            _fmt(metrics_b["false_positive_rate"]),
            _fmt(metrics_b["false_negative_rate"]),
        )
    )
    flip = audit["flips"]
    lines.append(
        "- 两臂**判定结果**翻转 **{}** 个窗口（{}）。".format(
            flip["total"], flip["detail"]
        )
    )
    failed = [
        int(item["id"])
        for item in results.values()
        if not item.get("ok")
    ]
    if failed:
        lines.append(
            "- 有 **{}** 次调用失败/降级，**未计入**指标（{}）。".format(
                len(failed),
                "、".join("#{}".format(i) for i in sorted(failed)),
            )
        )
        lines.append(
            "- 因此 A 臂实际计入 **{}** 个窗口、B 臂实际计入 **{}** 个窗口，"
            "不是全部 {} 个。".format(
                metrics_a["n"], metrics_b["n"], len(rows)
            )
        )
    elif len(results) == len(rows) * len(ARMS):
        lines.append(
            "- 全部 **{}** 次调用均成功，无降级、无缺项。".format(len(results))
        )
    else:
        lines.append(
            "- 已完成的 **{}** 次调用均成功，无降级；"
            "本轮共需 **{}** 次，还有 **{}** 次未跑"
            "（未跑的**不计入**指标）。".format(
                len(results),
                len(rows) * len(ARMS),
                len(rows) * len(ARMS) - len(results),
            )
        )
    lines.append("")
    lines.append("## 2. 评测设计")
    lines.append("")
    lines.append("| 项 | 取值 |")
    lines.append("| --- | --- |")
    for key, value in provider_info.items():
        lines.append("| {} | {} |".format(key, value))
    lines.append("")
    lines.append(
        "**只换【最近步骤】那一段的生成方式**：A 臂 = "
        "`build_window_summary(events)`，B 臂 = "
        "`build_compressed_summary(events, goal)`。"
        "目标文本、System Prompt、模型、温度、max_tokens、"
        "脱敏与解析全部共用生产代码路径。"
    )
    lines.append("")
    lines.append("### 2.1 「只换一段」的机器校验")
    lines.append("")
    lines.append(
        "- 校验方式：把 A 臂实际发出的 prompt 里的【最近步骤】段"
        "替换成 B 臂的那一段，再与 B 臂实际发出的 prompt **逐字节**"
        "比较。{}。".format(audit["detail"])
    )
    lines.append(
        "- 输入保真：{}。".format(audit["fidelity_detail"])
    )
    if audit.get("surgery_skipped"):
        lines.append(
            "- ⚠️ 有 **{}** 个窗口**没有 prompt 记录**（未调用或未落盘），"
            "这些窗口**没做**手术校验。".format(audit["surgery_skipped"])
        )
    lines.append("")
    lines.append("## 3. 真值口径")
    lines.append("")
    lines.append(
        "- 判据：{}".format(audit["criteria"])
    )
    lines.append(
        "- 真值分布：常规演进 **{}** 个，真实偏离 **{}** 个。".format(
            metrics_a["routine_windows"], metrics_a["drift_windows"]
        )
    )
    lines.append(
        "- 与历史模型判定的分歧：**{}** 个窗口（详见 §5）。".format(
            audit["divergence_count"]
        )
    )
    lines.append("")
    lines.append("## 4. 指标对照")
    lines.append("")
    lines.append("| 口径 | 臂 | 准确率 | 误报率 | 漏报率 | TP | FP | TN | FN |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for caliber, arm_label, data in (
        ("锚定口径（主）", "A 旧格式", metrics_a),
        ("锚定口径（主）", "B 压缩格式", metrics_b),
        ("会话口径（敏感性）", "A 旧格式", alt_a),
        ("会话口径（敏感性）", "B 压缩格式", alt_b),
        ("历史判定（对照）", "—", metrics_hist),
    ):
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                caliber,
                arm_label,
                _fmt(data["accuracy"]),
                _fmt(data["false_positive_rate"]),
                _fmt(data["false_negative_rate"]),
                data["tp"],
                data["fp"],
                data["tn"],
                data["fn"],
            )
        )
    lines.append("")
    lines.append("## 5. 逐窗口明细")
    lines.append("")
    lines.append(
        "| # | line | 目标 | 真值 | 类别 | 历史 | A 臂 | B 臂 | 一致 | 说明 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        window_id = int(row["id"])
        item_a = results.get((window_id, "A"))
        item_b = results.get((window_id, "B"))
        drift_a = (
            ("✅true" if item_a["drift"] else "✅false")
            if item_a and item_a.get("ok")
            else "—"
        )
        drift_b = (
            ("✅true" if item_b["drift"] else "✅false")
            if item_b and item_b.get("ok")
            else "—"
        )
        same = "=" if drift_a == drift_b else "≠"
        lines.append(
            "| {} | {} | g{} | {} | {} | {} | {} | {} | {} | {} |".format(
                window_id,
                row["line"],
                row["goal_id"],
                "偏离" if row["truth_drift"] else "常规",
                row["category"],
                "T" if row["historical_drift"] else "F",
                drift_a,
                drift_b,
                same,
                row["rationale"],
            )
        )
    lines.append("")
    lines.append("## 6. 已知边界")
    lines.append("")
    lines.extend(
        "- {}".format(item) for item in audit["boundaries"]
    )
    lines.append("")

    with io.open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def build_audit(
    rows: List[Dict[str, Any]],
    truth: Dict[int, bool],
    alt_truth: Dict[int, bool],
    prompts: Dict[Tuple[int, str], Tuple[str, str]],
) -> Dict[str, Any]:
    """做 prompt 手术校验与统计。

    :param rows: 夹具行。
    :param truth: 主口径真值。
    :param alt_truth: 会话口径真值。
    :param prompts: ``(窗口 id, 臂名) -> (system, user)``。
    :return: 审计结果。
    """
    surgery_ok = 0
    surgery_bad: List[int] = []
    surgery_skipped = 0
    fidelity_ok = sum(1 for row in rows if row.get("fidelity_ok"))
    for row in rows:
        window_id = int(row["id"])
        item_a = prompts.get((window_id, "A"))
        item_b = prompts.get((window_id, "B"))
        if not item_a or not item_b:
            surgery_skipped += 1
            continue
        summary_a = arm_summary(row, "A")
        summary_b = arm_summary(row, "B")
        patched = item_a[1].replace(summary_a, summary_b, 1)
        if patched == item_b[1]:
            surgery_ok += 1
        else:
            surgery_bad.append(window_id)

    divergence = [
        int(row["id"]) for row in rows
        if bool(row["historical_drift"]) != bool(row["truth_drift"])
    ]
    return {
        "surgery_ok": surgery_ok,
        "surgery_bad": surgery_bad,
        "surgery_skipped": surgery_skipped,
        "fidelity_ok": fidelity_ok,
        "fidelity_detail": "{} / {} 个窗口的反解事件能**逐字节**还原"
        "日志里当初发出的 A 臂摘要".format(fidelity_ok, len(rows)),
        "detail": "{} / {} 个窗口通过；未通过的窗口：{}".format(
            surgery_ok, len(rows), surgery_bad or "无"
        ),
        "criteria": "窗口内的动作是否仍在服务【该窗口所锚定的 200 字"
        "目标】声明的范围。锚点点名的子任务 → 常规；锚点完全没提的"
        "新主题 → 真实偏离；锚点没提但属于常规铺垫（改 CHANGELOG / "
        "版本号 bump / 写测试 / 跑验证）→ 常规。",
        "divergence_count": len(divergence),
        "divergence_ids": divergence,
        "boundaries": [],
    }


def judgment_flips(
    rows: List[Dict[str, Any]],
    pred_a: Dict[int, bool],
    pred_b: Dict[int, bool],
) -> Dict[str, Any]:
    """统计两臂**判定结果**翻转的窗口。

    注意：两臂的 user prompt 文本本来就必然不同（这正是实验变量），
    所以「翻转」只能按 ``drift`` 判定结果来数，不能拿 prompt 文本
    差异当翻转。

    :param rows: 夹具行。
    :param pred_a: A 臂预测。
    :param pred_b: B 臂预测。
    :return: ``{"total": int, "ids": [...], "detail": str}``。
    """
    ids: List[int] = []
    for row in rows:
        window_id = int(row["id"])
        if window_id not in pred_a or window_id not in pred_b:
            continue
        if pred_a[window_id] != pred_b[window_id]:
            ids.append(window_id)
    detail = (
        "窗口 {}".format("、".join(str(i) for i in ids)) if ids else "无"
    )
    return {"total": len(ids), "ids": ids, "detail": detail}


def main() -> int:
    """主流程。

    :return: 进程退出码。
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = load_fixture()
    done = load_done()
    truth: Dict[int, bool] = {}
    alt_truth: Dict[int, bool] = {}
    for row in rows:
        window_id = int(row["id"])
        truth[window_id] = bool(row["truth_drift"])
        alt_truth[window_id] = bool(row["truth_drift"])
        if int(row["goal_id"]) == 3:
            alt_truth[window_id] = False

    provider, captured = make_provider()
    prompts: Dict[Tuple[int, str], Tuple[str, str]] = load_prompts()
    results = dict(done)
    handle = io.open(RESULTS_PATH, "a", encoding="utf-8", newline="\n")
    try:
        for row in rows:
            window_id = int(row["id"])
            order = list(ARMS)
            if window_id % 2 == 0:
                order.reverse()
            for arm in order:
                if (window_id, arm) in results:
                    continue
                before = len(captured)
                outcome = run_arm(provider, row, arm)
                if len(captured) > before:
                    prompts[(window_id, arm)] = captured[-1]
                    outcome["prompt_system"] = captured[-1][0]
                    outcome["prompt_user"] = captured[-1][1]
                handle.write(
                    json.dumps(outcome, ensure_ascii=False) + "\n"
                )
                handle.flush()
                results[(window_id, arm)] = outcome
                print(
                    "[{}] arm={} drift={} {}ms".format(
                        window_id,
                        arm,
                        outcome["drift"],
                        outcome["latency_ms"],
                    ),
                    flush=True,
                )
    finally:
        handle.close()

    provider_info = {
        "模型": getattr(provider, "model", "?"),
        "提供方": getattr(provider, "name", "?"),
        "base_url": getattr(provider, "base_url", "?"),
        "temperature": getattr(provider, "temperature", "?"),
        "max_tokens": getattr(provider, "max_tokens", "?"),
        "json_mode": getattr(provider, "json_mode", "?"),
        "redact_before_llm": getattr(
            provider, "redact_before_llm", "?"
        ),
        "System Prompt": "src.llm.prompts.DRIFT_EVALUATE_TEMPLATE（两臂共用）",
        "窗口数": len(rows),
        "调用次数": len(results),
    }
    audit = build_audit(rows, truth, alt_truth, prompts)
    audit["boundaries"] = [
        "本评测是**纯 L2 对比** —— 生产链路里 "
        "`detect_routine_maintenance()` 白名单在**发请求之前**就短路，"
        "命中即判 `drift=False`，根本不会调用模型。"
        "白名单在本次 41 个窗口里命中 {} 个（见下表）。".format(
            sum(1 for row in rows if row.get("whitelist_hit"))
        ),
        "A 臂输入来自真实生产日志里**当初实际发出**的 user prompt；"
        "事件序列是由该 prompt 反解出来的，反解结果能逐字节还原"
        "原 prompt（A 臂就是这份 prompt 的摘要段），"
        "因此 A 臂与历史判定**同源**。",
        "B 臂的输入由同一批反解事件经 `build_compressed_summary()` 生成，"
        "压缩器读的是事件里的 `thinking` 正文与工具参数 —— "
        "反解事件里的工具参数是从摘要文本还原的，"
        "**比真实事件少**（真实事件里的工具参数可能不止摘要里那一个）。",
        "本机 NInfer 是**单并发**，两臂调用严格串行；"
        "调用顺序逐窗交替以抵消顺序偏差，但**无法完全排除**"
        "引擎内部缓存/前缀缓存带来的微小差异。",
        "样本只有 41 个窗口，且全部来自同一份真实日志 —— "
        "结论的统计效力有限，只能支撑「有没有退化」这一量级的判断。",
    ]
    pred_a = _predict(results, "A")
    pred_b = _predict(results, "B")
    audit["flips"] = judgment_flips(rows, pred_a, pred_b)
    failed = [
        {
            "id": int(item["id"]),
            "arm": item["arm"],
            "error": item.get("error", ""),
        }
        for item in results.values()
        if not item.get("ok")
    ]
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": provider_info,
        "audit": audit,
        "metrics": {
            "anchor_A": metrics(rows, truth, pred_a),
            "anchor_B": metrics(rows, truth, pred_b),
            "session_A": metrics(rows, alt_truth, pred_a),
            "session_B": metrics(rows, alt_truth, pred_b),
            "historical": metrics(rows, truth, {
                int(row["id"]): bool(row["historical_drift"])
                for row in rows
            }),
        },
        "whitelist_hits": [
            {
                "id": int(row["id"]),
                "line": row["line"],
                "reason": row["whitelist_hit"],
            }
            for row in rows if row.get("whitelist_hit")
        ],
        "failed_calls": failed,
        "results": [
            results[key] for key in sorted(results.keys())
        ],
    }
    with io.open(METRICS_PATH, "w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(payload, ensure_ascii=False, indent=2))
    write_report(rows, results, truth, alt_truth, provider_info, audit)
    print("metrics ->", METRICS_PATH)
    print("report  ->", REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
