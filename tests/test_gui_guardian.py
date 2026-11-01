"""哨兵 GUI —— 守护者干预端点测试。

面板层**不认识**守护者：它只依赖一个鸭子类型对象（实现
``status`` / ``intervene`` / ``override`` 三个方法）。这里的假守护者
只记录调用，验证接口契约与异常隔离。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi.testclient import TestClient

from src.gui.server import create_app


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


def _client(
    tmp_path: Any,
    guardian: Any = None,
) -> TestClient:
    """构造一个测试客户端。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alert.log"
    alert_log.write_text("", encoding="utf-8")
    app = create_app(
        str(watch),
        str(alert_log),
        guardian_callback=guardian,
        start_watcher=False,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
class TestGuardianStatus:
    """GET /api/guardian/status"""

    def test_should_report_unavailable_without_guardian(
        self, tmp_path: Any
    ) -> None:
        """没接守护者时如实报告「不可用」。"""
        with _client(tmp_path) as client:
            body = client.get("/api/guardian/status").json()
        assert body["available"] is False
        assert body["enabled"] is False

    def test_should_return_guardian_state(self, tmp_path: Any) -> None:
        """接了守护者就返回它的状态。"""
        guardian = FakeGuardian()
        with _client(tmp_path, guardian) as client:
            body = client.get("/api/guardian/status").json()
        assert body["available"] is True
        assert body["enabled"] is True
        assert body["target_pid"] == 4242

    def test_should_not_crash_when_guardian_raises(
        self, tmp_path: Any
    ) -> None:
        """守护者抛异常不能让看板挂掉。"""
        guardian = FakeGuardian(fail=True)
        with _client(tmp_path, guardian) as client:
            response = client.get("/api/guardian/status")
        assert response.status_code == 500
        assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# 强制中断
# ---------------------------------------------------------------------------
class TestInterveneEndpoint:
    """POST /api/guardian/intervene"""

    def test_should_forward_payload_to_guardian(self, tmp_path: Any) -> None:
        """按钮点下去的内容要原样送到守护者。"""
        guardian = FakeGuardian()
        with _client(tmp_path, guardian) as client:
            response = client.post(
                "/api/guardian/intervene",
                json={
                    "rule_id": "catastrophic_root_delete",
                    "reason": "红线级命中",
                    "command": "rm -rf /",
                    "source": "gui",
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["method"] == "suspend"
        assert guardian.calls
        assert guardian.calls[0]["rule_id"] == "catastrophic_root_delete"
        assert guardian.calls[0]["source"] == "gui"

    def test_should_return_503_without_guardian(self, tmp_path: Any) -> None:
        """没接守护者时明确报 503。"""
        with _client(tmp_path) as client:
            response = client.post(
                "/api/guardian/intervene", json={"rule_id": "x"}
            )
        assert response.status_code == 503
        assert response.json()["ok"] is False

    def test_should_return_500_when_guardian_raises(
        self, tmp_path: Any
    ) -> None:
        """守护者异常要被隔离成 500，而不是 500 之外的东西。"""
        guardian = FakeGuardian(fail=True)
        with _client(tmp_path, guardian) as client:
            response = client.post(
                "/api/guardian/intervene", json={"rule_id": "x"}
            )
        assert response.status_code == 500
        assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# 放行并忽略
# ---------------------------------------------------------------------------
class TestOverrideEndpoint:
    """POST /api/guardian/override"""

    def test_should_forward_payload_to_guardian(self, tmp_path: Any) -> None:
        """放行请求要原样送到守护者。"""
        guardian = FakeGuardian()
        with _client(tmp_path, guardian) as client:
            response = client.post(
                "/api/guardian/override",
                json={
                    "rule_id": "catastrophic_root_delete",
                    "source": "gui",
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["resumed"] is True
        assert guardian.overrides
        assert guardian.overrides[0]["rule_id"] == "catastrophic_root_delete"

    def test_should_return_503_without_guardian(self, tmp_path: Any) -> None:
        """没接守护者时明确报 503。"""
        with _client(tmp_path) as client:
            response = client.post(
                "/api/guardian/override", json={"rule_id": "x"}
            )
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# 跨站防护
# ---------------------------------------------------------------------------
class TestOriginGuard:
    """干预端点同样要受 Origin 白名单保护。"""

    def test_should_reject_external_origin(self, tmp_path: Any) -> None:
        """外部 Origin 一律 403 —— 干预能力不能被跨站触发。"""
        guardian = FakeGuardian()
        with _client(tmp_path, guardian) as client:
            response = client.post(
                "/api/guardian/intervene",
                json={"rule_id": "x"},
                headers={"Origin": "http://evil.example.com"},
            )
        assert response.status_code == 403
        assert guardian.calls == [], "跨站请求不该触达守护者"

    def test_should_reject_external_origin_on_override(
        self, tmp_path: Any
    ) -> None:
        """放行端点同样受保护。"""
        guardian = FakeGuardian()
        with _client(tmp_path, guardian) as client:
            response = client.post(
                "/api/guardian/override",
                json={"rule_id": "x"},
                headers={"Origin": "http://evil.example.com"},
            )
        assert response.status_code == 403
        assert guardian.overrides == []
