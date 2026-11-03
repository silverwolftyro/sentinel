"""哨兵 GUI —— 守护者干预端点测试。

面板层**不认识**守护者：它只依赖一个鸭子类型对象（实现
``status`` / ``intervene`` / ``override`` 三个方法）。这里的假守护者
只记录调用，验证接口契约与异常隔离。
"""

from __future__ import annotations

from typing import Any, Dict, List


class FakeGuardian:
    """假守护者 —— 只记录调用，不碰任何进程。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.overrides: List[Dict[str, Any]] = []
        self.fail = fail

    def status(self) -> Dict[str, Any]:
        """返回状态快照。"""
        if self.fail:
            raise RuntimeError("守护者炸了")
        return {
            "enabled": True,
            "mode": "guardian",
            "target_pid": 4242,
            "suspended_pids": [4242],
            "overridden_rules": [],
        }

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次干预请求。"""
        if self.fail:
            raise RuntimeError("守护者炸了")
        self.calls.append(payload)
        return {
            "ok": True,
            "message": "[哨兵已拦截] 发现高危操作，已暂停目标进程。",
            "rule_id": payload.get("rule_id"),
            "pid": 4242,
            "method": "suspend",
        }

    def override(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次放行请求。"""
        if self.fail:
            raise RuntimeError("守护者炸了")
        self.overrides.append(payload)
        return {
            "ok": True,
            "message": "[哨兵已放行] 规则已从本次任务中移除",
            "rule_id": payload.get("rule_id"),
            "pid": 4242,
            "resumed": True,
        }




# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
class TestGuardianStatus:
    """GET /api/guardian/status"""





# ---------------------------------------------------------------------------
# 强制中断
# ---------------------------------------------------------------------------
class TestInterveneEndpoint:
    """POST /api/guardian/intervene"""





# ---------------------------------------------------------------------------
# 放行并忽略
# ---------------------------------------------------------------------------
class TestOverrideEndpoint:
    """POST /api/guardian/override"""




# ---------------------------------------------------------------------------
# 跨站防护
# ---------------------------------------------------------------------------
class TestOriginGuard:
    """干预端点同样要受 Origin 白名单保护。"""
