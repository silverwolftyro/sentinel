"""v4.0.1：压缩格式的**信息保真度**回归。

背景
====

v4.0.0 把 L2 的输入从 :func:`src.core.engine.build_window_summary`
换成了 :func:`src.core.engine.build_compressed_summary`，
但**没有重跑**误报评测。v4.0.1 重跑 41 个历史漂移窗口 × 2 臂后发现：

* **B 臂（压缩格式）漏报率从 24.0% 恶化到 40.0%**，
  准确率从 80.49% 掉到 75.61% —— 这是**实打实的退化**，不是错觉；
* 根因有三条，全部钉在压缩器里：

  1. **转向枢轴句被当废话丢掉** ——
     「Now let me generate the synthetic "approaching" material.」
     句首带 ``let me``（语气词表命中），动词 ``generate`` 又不在
     意图动词表里 → 整句被丢 → L2 看不到 Agent「转去做什么」；
  2. **结构化事实句被丢掉** ——
     「Key facts: 1. **Webhook path**: engine → `_build_alert` …」
     没有动词（名词化的「路径 / 理由 / 要求」），却装着 L2 要看的证据；
  3. **目标正文漏进演进流** ——
     目标文本带换行时，``[Task Goal]:`` 锚定行被撑成多行，
     引擎侧只剥首行 → 目标的正文（需求清单）混进了演进流。

本文件把三条修复**钉死**，并用 41 窗口夹具做**端到端**核对 ——
不调模型，纯输入输出比对。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

from src.core.engine import (
    build_compressed_summary,
    build_window_summary,
)
from src.core.log_compressor import (
    CompressionOptions,
    compress_events,
    extract_intent,
)

#: 仓库根目录（本文件在 ``tests/`` 下）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 41 个历史漂移窗口的夹具（反解自真实生产日志，逐字节可还原）。
FIXTURE_PATH = os.path.join(
    ROOT, "tests", "fixtures", "v401_drift_windows.jsonl"
)

#: 回归重点窗口 —— v4.0.1 首轮 A/B 里 B 臂漏报、A 臂命中的那 6 个。
REGRESSION_WINDOWS: Dict[int, str] = {
    21: "_build_alert",
    27: "generate the synthetic",
    30: "compute the evasion direction",
    32: "警告：前方 300 像素有玩家逼近",
    34: "玩家追踪",
    38: "Let me recompute.",
}


def _load_rows() -> List[Dict]:
    """读取 41 窗口夹具。

    :return: 夹具行列表。
    """
    rows: List[Dict] = []
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


# ---------------------------------------------------------------------------
# 修复一：转向枢轴句不能被当废话丢掉
# ---------------------------------------------------------------------------
def test_英文转向句不再被当废话丢掉():
    """「Now let me generate …」是枢轴信号，必须留下。"""
    text = (
        'Now let me generate the synthetic "approaching" material. '
        "Let me write a script that: 1. Takes frame_000001.jpg"
    )
    intent = extract_intent(text)
    assert "generate the synthetic" in intent
    assert "approaching" in intent


def test_前缀粘连词能命中():
    """``recompute`` / ``regenerate`` 在 ``\\b`` 语义下匹配不到词根。"""
    assert "recompute" in extract_intent("Let me recompute.")
    assert "regenerate" in extract_intent("Let me regenerate the fixture.")


def test_中文单字动词不误伤():
    """加了英文动词表，不能把中文语气词当成关键句留下。"""
    text = (
        "嗯，让我先看看这个文件到底是怎么实现的。\n"
        "我想我需要确认一下现有的实现是不是真的有问题。\n"
        "好的，接下来我要修改 src/core/auth.py 里的过期判断逻辑。\n"
        "让我再确认一下刚才那处改动是不是生效了。"
    )
    intent = extract_intent(text)
    assert "修改" in intent
    assert "src/core/auth.py" in intent
    assert "让我先看看" not in intent
    assert "确认一下" not in intent


def test_纯废话仍然整条丢掉():
    """保真度不能靠「什么都留」换来 —— 纯废话仍然要丢。"""
    assert extract_intent("嗯，好的，哦，是的") == ""


# ---------------------------------------------------------------------------
# 修复二：结构化事实句要留下
# ---------------------------------------------------------------------------
def test_结构化事实句被保留():
    """「Key facts: 1. Webhook path: …」没有动词，但装着证据。"""
    text = (
        "Excellent. Now I have all the information needed. "
        "Let me plan and start implementing. "
        "Key facts: 1. **Webhook path**: engine → `_build_alert` → "
        "`_publish_alert` → `_record_alert`"
    )
    intent = extract_intent(text)
    assert "Webhook path" in intent
    assert "_build_alert" in intent


def test_以列表符号开头的句子被保留():
    """「- 77 candidates over 92 sampled frames」也是事实句。"""
    text = (
        "Interesting. So after filtering: "
        "- 77 candidates over 92 sampled frames. "
        "- 22 survive the visor cue."
    )
    intent = extract_intent(text)
    assert "77 candidates" in intent


# ---------------------------------------------------------------------------
# 修复三：目标正文不许漏进演进流
# ---------------------------------------------------------------------------
def test_目标含换行时锚定行仍是一行():
    """``[Task Goal]:`` 锚定行必须只有一行。"""
    goal = "发布 v4.0.1\n\n1. 第一项\n2. 第二项\n3. 第三项"
    events = [{"type": "thinking", "text": "Now let me fix the auth module."}]
    result = compress_events(events, options=CompressionOptions(goal=goal))
    head = result.text.splitlines()[0]
    assert head.startswith("[Task Goal]: ")
    assert "第一项" in head
    # 目标里的换行不许把锚定行撑成多行
    assert len(result.text.splitlines()[0].splitlines()) == 1
    assert result.text.splitlines()[0].count("\n") == 0


def test_目标正文不泄漏到演进流():
    """引擎侧剥掉锚定行后，目标的正文不许残留在摘要里。"""
    goal = (
        "具体要求：\n1. 鼠标移动：废弃瞬间 SetCursorPos\n"
        "2. 提供 hover 提示\n3. 保持 60fps"
    )
    summary = build_compressed_summary(
        [
            {
                "type": "thinking",
                "text": "Now let me generate the synthetic material.",
            }
        ],
        goal,
    )
    assert "具体要求" not in summary
    assert "SetCursorPos" not in summary
    assert "hover 提示" not in summary
    assert "generate the synthetic" in summary


# ---------------------------------------------------------------------------
# 端到端：41 窗口夹具核对
# ---------------------------------------------------------------------------
def test_夹具本身是完好的():
    """夹具必须 41 行、逐字节可还原 —— 前提不成立后面都白测。"""
    rows = _load_rows()
    assert len(rows) == 41
    assert all(row.get("fidelity_ok") for row in rows)
    assert sum(1 for row in rows if row["truth_drift"]) == 25
    assert sum(1 for row in rows if not row["truth_drift"]) == 16


def test_重点窗口的枢轴信号没有丢():
    """v4.0.1 首轮 B 臂漏报的那 6 个窗口，证据必须还在摘要里。"""
    rows = {int(row["id"]): row for row in _load_rows()}
    for window_id, needle in REGRESSION_WINDOWS.items():
        row = rows[window_id]
        summary = build_compressed_summary(
            row.get("events", []), str(row.get("goal") or "")
        )
        assert needle in summary, (
            "窗口 {} 的压缩摘要丢了枢轴信号 {!r}".format(window_id, needle)
        )


def test_全部窗口都不漏目标正文():
    """41 个窗口逐个核对：目标正文不许漏进演进流。"""
    for row in _load_rows():
        goal = " ".join(str(row.get("goal") or "").split())
        summary = build_compressed_summary(row.get("events", []), goal)
        assert goal[:24] not in summary, (
            "窗口 {} 的目标正文漏进了演进流".format(row.get("id"))
        )


def test_压缩摘要长度没有失控():
    """加了事实句之后，每步预算仍是 120 字，总长不能炸。"""
    for row in _load_rows():
        summary = build_compressed_summary(
            row.get("events", []), str(row.get("goal") or "")
        )
        assert len(summary) <= 8000
        assert len(summary) <= len(build_window_summary(row.get("events", []))) + 80
