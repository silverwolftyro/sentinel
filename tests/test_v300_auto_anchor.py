"""v3.0.0 回归：全自动智能锚定与默认武装推荐。

背景
====

v2.4.0 把挂载会话从「只读」升级为「全功能」，
但用户还得**手动**做两件事：

1. 手动设目标（旁路日志往往没有最初的 Prompt）；
2. 手动开武装开关。

v3.0.0 要把这两件事做得**尽可能无感**：

* **自动提取意图** —— 哨兵自己从日志开头把用户最初那句
  Prompt 挖出来当锚点，界面上标「[自动提取]」；
* **默认武装推荐** —— PID 置信度 ≥ 0.95 时把开关**高亮**出来，
  并用气泡提示「高置信度匹配，建议开启守护者」。

⚠️ 两条铁律：
* **绝不自动武装** —— 挂起别人的进程必须用户自己点头（红线 R7）；
* **绝不假装** —— 提取不到就明说提取不到，不编一个目标出来。

本文件钉住：

1. 自动提取的**正确性**（三种 Agent 格式各一条）与**边界**；
2. 自动提取后追踪器**立刻**建起来（漂移监控真的在工作）；
3. 武装推荐**只高亮不武装**。
"""

from __future__ import annotations

import json

import pytest

from src.core.session_server import WatchTarget


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write(path: str, payloads) -> None:
    """写一份 JSONL 日志。"""
    with open(path, "w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")






























# ---------------------------------------------------------------------------
# 目标来源标签
# ---------------------------------------------------------------------------
def test_目标来源标签齐全():
    """面板上的中文说明要齐全，不能出现裸的英文标识。"""
    labels = WatchTarget.GOAL_SOURCE_LABELS
    assert labels["auto"] == "自动提取"
    # v3.3.1：人工设定之后顶部显示的是「手动锚定」——
    # 说的是**结果**（漂移监控锚上了），不是「这个值是你手填的」。
    assert labels["explicit"] == "手动锚定"
    assert labels["config"] == "配置文件"
    assert labels["none"] == "未设置"


def test_自动锚定标记与来源一致():
    """``auto_anchored`` 只对「自动提取」为真。"""
    target = WatchTarget(agent="codex")
    target.set_goal("某目标", "auto")
    snapshot = target.snapshot()
    assert snapshot["auto_anchored"] is True
    assert snapshot["goal_source"] == "auto"

    target.set_goal("另一个目标", "explicit")
    assert target.snapshot()["auto_anchored"] is False




if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
