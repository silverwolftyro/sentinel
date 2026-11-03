"""哨兵 GUI **多路会话中枢**测试（v1.0.0）。

验证四件事：

1. ``/api/sessions`` 能列出全部会话及其状态；
2. 会话级接口（告警 / 目标 / 暂停 / 守护者）**各管各的**，
   不会串台 —— 这是多路中枢最容易出的错；
3. SSE 事件带 ``session_id`` —— 前端据此分流；
4. v0.x 的单会话用法**完全不受影响**（老接口还在、老行为不变）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class _FakeGuardian:
    """守护者替身 —— 记录自己被谁调用过。"""

    def __init__(self, name: str = "g") -> None:
        self.name = name
        self.calls: List[str] = []
        self.overridden: Dict[str, Any] = {}

    def status(self) -> Dict[str, Any]:
        """返回守护者状态。"""
        self.calls.append("status")
        return {
            "ok": True,
            "enabled": True,
            "available": True,
            "name": self.name,
        }

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次干预。"""
        self.calls.append("intervene")
        return {"ok": True, "method": "suspend", "pid": 1234}

    def override(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次放行。"""
        self.calls.append("override")
        self.overridden = dict(payload)
        return {"ok": True, "resumed": True, "rule_id": payload.get("rule_id")}


def _write_alert(path: Path, alert_id: str, rule: str) -> None:
    """往告警日志里写一条机器行告警。"""
    payload = {
        "alert_id": alert_id,
        "timestamp": 1793070000.5,
        "verdict": "DANGEROUS",
        "level": "critical",
        "rule_id": rule,
        "reason": "红线级命中",
        "command": "rm -rf /tmp/x",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            "2026-10-27 10:00:00,000 WARNING ALERT_JSON "
            + json.dumps(payload, ensure_ascii=False)
            + "\n"
        )




# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------
class TestSessionList:
    """/api/sessions —— 全局总览。"""





# ---------------------------------------------------------------------------
# 会话级接口：各管各的
# ---------------------------------------------------------------------------
class TestSessionIsolation:
    """多路中枢最容易出的错是「串台」—— 必须各管各的。"""








# ---------------------------------------------------------------------------
# SSE 事件分流
# ---------------------------------------------------------------------------
class TestSessionEventRouting:
    """SSE 事件必须带 session_id —— 前端据此分流。"""







# ---------------------------------------------------------------------------
# 会话模型
# ---------------------------------------------------------------------------
class TestSessionModel:
    """Session / SessionRegistry 的行为。"""
