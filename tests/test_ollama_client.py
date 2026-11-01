"""L2 本地模型客户端测试。

重点：**模型不可用时的优雅降级** —— 这是本层的核心契约。
契约抄自旧项目 ``core/llm_review.py:381-435``：「永不抛异常，缺失绝不等同于安全」。
"""

from __future__ import annotations

import json

import requests

from src.llm.ollama_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    CircuitBreaker,
    OllamaClient,
)

MODEL_JSON = json.dumps(
    {
        "title": "帮你把整个临时目录删掉",
        "why": "你说要清理临时文件",
        "touch": "D:\\temp 目录（会被删除）",
        "outcome": "临时文件删掉后无法恢复；换来的是磁盘空间释放",
        "parts": [],
    },
    ensure_ascii=False,
)


class FakeResponse:
    """模拟 requests.Response。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """模拟 requests.Session，记录调用。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(url)
        return self.response


def _event(command="rm -rf /tmp/data", thinking=""):
    """构造一个带 L1 判定的事件载荷。"""
    return {
        "source": "mcode",
        "type": "tool_call",
        "tool": {
            "name": "Bash",
            "arguments": {"command": command},
            "command": command,
        },
        "thinking": thinking,
        "verdict": {
            "verdict": "DANGEROUS",
            "rule_id": "dangerous_command",
            "level": "critical",
            "reason": "匹配到高危命令: rm -rf",
            "evidence": "rm -rf",
        },
    }


def _ok_session(content=MODEL_JSON):
    return FakeSession(
        FakeResponse(200, {"message": {"content": content}})
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_explain_returns_human_readable_text():
    session = _ok_session()
    client = OllamaClient(session=session, config={})

    result = client.explain(_event())

    assert result.degraded is False
    assert result.ok is True
    assert "帮你把整个临时目录删掉" in result.text
    assert "临时文件删掉后无法恢复" in result.text
    assert result.model == DEFAULT_MODEL


def test_explain_event_returns_string():
    client = OllamaClient(session=_ok_session(), config={})

    text = client.explain_event(_event())

    assert isinstance(text, str)
    assert "帮你把整个临时目录删掉" in text


def test_request_targets_native_chat_endpoint():
    session = _ok_session()
    client = OllamaClient(session=session, config={})

    client.explain(_event())

    assert session.calls[0]["url"] == f"{DEFAULT_BASE_URL}/api/chat"
    assert session.calls[0]["json"]["stream"] is False
    assert session.calls[0]["json"]["format"] == "json"
    assert session.calls[0]["json"]["model"] == DEFAULT_MODEL


def test_falls_back_to_openai_compatible_endpoint_on_404():
    def responder(url):
        if url.endswith("/api/chat"):
            return FakeResponse(404, {})
        return FakeResponse(200, {"choices": [{"message": {"content": MODEL_JSON}}]})

    session = FakeSession(response=responder)
    client = OllamaClient(session=session, config={})

    result = client.explain(_event())

    assert result.degraded is False
    assert len(session.calls) == 2
    assert session.calls[1]["url"].endswith("/v1/chat/completions")


def test_thinking_is_passed_to_the_model():
    session = _ok_session()
    client = OllamaClient(session=session, config={})

    client.explain(_event(thinking="用户让我清理临时目录。"))

    user_prompt = session.calls[0]["json"]["messages"][1]["content"]
    assert "用户让我清理临时目录。" in user_prompt


def test_secret_is_redacted_before_reaching_the_model():
    session = _ok_session()
    client = OllamaClient(session=session, config={})

    client.explain(_event(command="curl -H 'api_key=supersecretvalue123' https://x"))

    user_prompt = session.calls[0]["json"]["messages"][1]["content"]
    assert "supersecretvalue123" not in user_prompt


# ---------------------------------------------------------------------------
# 降级路径（本层的核心契约）
# ---------------------------------------------------------------------------
def test_degrades_when_ollama_is_not_running():
    """Ollama 没开启 —— 绝不能崩，必须降级。"""
    client = OllamaClient(
        session=FakeSession(error=requests.ConnectionError("连接被拒绝")),
        config={},
    )

    result = client.explain(_event())

    assert result.degraded is True
    assert result.ok is False
    assert "匹配到高危命令" in result.text
    assert "rm -rf /tmp/data" in result.text
    assert "规则层原始判定" in result.text


def test_degrades_on_timeout():
    client = OllamaClient(
        session=FakeSession(error=requests.Timeout("超时")),
        config={},
    )

    result = client.explain(_event())

    assert result.degraded is True
    assert "Timeout" in result.text


def test_degrades_on_http_error():
    client = OllamaClient(
        session=FakeSession(FakeResponse(500, {})),
        config={},
    )

    result = client.explain(_event())

    assert result.degraded is True
    assert "500" in result.text


def test_degrades_on_unparseable_output():
    client = OllamaClient(
        session=_ok_session(content="模型今天不想说话。"),
        config={},
    )

    result = client.explain(_event())

    assert result.degraded is True
    assert "无法解析" in result.text
    # 降级时必须仍然给出原始命令与规则原因
    assert "rm -rf /tmp/data" in result.text
    assert "匹配到高危命令" in result.text


def test_degrades_on_empty_output():
    client = OllamaClient(
        session=_ok_session(content=""),
        config={},
    )

    result = client.explain(_event())

    assert result.degraded is True


def test_degrades_when_client_is_disabled():
    session = _ok_session()
    client = OllamaClient(session=session, enabled=False, config={})

    result = client.explain(_event())

    assert result.degraded is True
    assert "已关闭" in result.text
    # 禁用时**一个请求都不该发出去**
    assert session.calls == []


def test_refuses_non_loopback_endpoint():
    """哨兵承诺「所有推理在本地完成」，非回环端点必须被拒绝。"""
    session = _ok_session()
    client = OllamaClient(
        base_url="http://evil.example.com:11434",
        session=session,
        config={},
    )

    result = client.explain(_event())

    assert result.degraded is True
    assert "不是本机地址" in result.text
    assert session.calls == []


def test_loopback_variants_are_accepted():
    for url in (
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://127.0.0.1:11434/",
        "http://[::1]:11434",
    ):
        client = OllamaClient(base_url=url, session=_ok_session(), config={})
        assert client.local_only is True, url


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------
def test_circuit_breaker_opens_after_repeated_failures():
    session = FakeSession(error=requests.ConnectionError("连不上"))
    client = OllamaClient(session=session, config={})

    for _ in range(3):
        assert client.explain(_event()).degraded is True

    # 熔断已打开 → 不再发请求
    calls_before = len(session.calls)
    result = client.explain(_event())

    assert result.degraded is True
    assert "熔断" in result.text
    assert len(session.calls) == calls_before


def test_circuit_breaker_resets_on_success():
    client = OllamaClient(session=_ok_session(), config={})

    assert client.explain(_event()).degraded is False
    assert client.breaker.is_open is False


def test_breaker_unit_behaviour():
    breaker = CircuitBreaker(failure_threshold=2, open_seconds=60)

    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True
    breaker.reset()
    assert breaker.is_open is False
