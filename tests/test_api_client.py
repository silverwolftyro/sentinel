"""通用 API 接入（OpenAI 兼容）测试。

**全部离线** —— 用假的 ``requests.Session`` 拦截请求，
断言「发出去的 URL / 请求头 / 请求体」与「解析回来的结构」。

重点验证三件事：

1. 请求确实按 OpenAI 兼容协议发出（URL、``Bearer`` 鉴权、消息体）；
2. 返回结构与本地 Ollama **完全一致**（标题 / 原因 / 影响 / 后果）；
3. 任何失败都**降级**而不是抛异常（红线 R5）。
"""

from __future__ import annotations

import json
import os

import requests

from src.llm.api_client import API_KEY_ENV, ApiError, OpenAICompatibleClient
from src.llm.base import Explanation
from src.llm.factory import build_provider, normalize_provider
from src.llm.ollama_client import OllamaClient

#: 一份合规的模型输出。
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


# ---------------------------------------------------------------------------
# 假会话
# ---------------------------------------------------------------------------
class FakeResponse:
    """模拟 requests.Response。"""

    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self) -> None:
        """模拟状态码检查。"""
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        """返回响应体。"""
        return self._payload


class FakeSession:
    """模拟 requests.Session，记录每次调用。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        """记录并返回一次「请求」。"""
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers or {},
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(url)
        return self.response


def _ok_session(content: str = MODEL_JSON) -> FakeSession:
    """一个正常返回的假会话。"""
    return FakeSession(
        FakeResponse(
            200,
            {"choices": [{"message": {"role": "assistant", "content": content}}]},
        )
    )


def _event(command: str = "rm -rf /tmp/data", thinking: str = "") -> dict:
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


def _client(session=None, **kwargs) -> OpenAICompatibleClient:
    """构造一个指向假服务的 API 客户端。"""
    config = {
        "llm": {
            "enabled": True,
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": "sk-test-key",
            "timeout": 30,
        }
    }
    return OpenAICompatibleClient(
        session=session or _ok_session(), config=config, **kwargs
    )


# ---------------------------------------------------------------------------
# 端点拼装
# ---------------------------------------------------------------------------
class TestEndpoint:
    """请求地址怎么算。"""

    def test_should_append_v1_chat_completions(self) -> None:
        """裸域名要补全标准路径。"""
        client = _client(base_url="https://api.deepseek.com")
        assert client.endpoint() == (
            "https://api.deepseek.com/v1/chat/completions"
        )

    def test_should_not_duplicate_v1(self) -> None:
        """已经带 /v1 的不要重复补。"""
        client = _client(base_url="https://api.deepseek.com/v1")
        assert client.endpoint() == (
            "https://api.deepseek.com/v1/chat/completions"
        )

    def test_should_keep_full_path(self) -> None:
        """写全路径就原样使用。"""
        client = _client(
            base_url="https://gw.example.com/v1/chat/completions"
        )
        assert client.endpoint() == (
            "https://gw.example.com/v1/chat/completions"
        )

    def test_should_support_local_gateway(self) -> None:
        """自建网关（vLLM / LM Studio）同样适用。"""
        client = _client(base_url="http://127.0.0.1:8000/v1")
        assert client.endpoint() == (
            "http://127.0.0.1:8000/v1/chat/completions"
        )

    def test_should_post_to_computed_endpoint(self) -> None:
        """实际请求必须打到算出来的地址。"""
        session = _ok_session()
        client = _client(session=session)
        client.explain(_event())
        assert session.calls[0]["url"] == (
            "https://api.deepseek.com/v1/chat/completions"
        )


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
class TestAuthentication:
    """API 密钥怎么用。"""

    def test_should_send_bearer_token(self) -> None:
        """有密钥就要带 Authorization 头。"""
        session = _ok_session()
        client = _client(session=session)
        client.explain(_event())
        headers = session.calls[0]["headers"]
        assert headers["Authorization"] == "Bearer sk-test-key"

    def test_should_prefer_env_var(self, monkeypatch) -> None:
        """环境变量优先于配置文件 —— 密钥不落盘是默认做法。"""
        monkeypatch.setenv(API_KEY_ENV, "sk-from-env")
        config = {
            "llm": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-from-config",
            }
        }
        client = OpenAICompatibleClient(config=config)
        assert client.api_key == "sk-from-env"

    def test_should_fall_back_to_config(self, monkeypatch) -> None:
        """环境变量没有时用配置里的。"""
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        client = _client()
        assert client.api_key == "sk-test-key"

    def test_should_omit_authorization_without_key(self) -> None:
        """没密钥就不发 Authorization 头（本地网关常见）。"""
        config = {
            "llm": {
                "enabled": True,
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "local-model",
            }
        }
        session = _ok_session()
        client = OpenAICompatibleClient(session=session, config=config)
        client.explain(_event())
        assert "Authorization" not in session.calls[0]["headers"]

    def test_should_not_leak_key_in_error_message(self) -> None:
        """密钥绝不能出现在错误文案里（错误会进日志）。"""
        session = FakeSession(
            FakeResponse(401, {}, text="invalid api key sk-test-key")
        )
        client = _client(session=session)
        result = client.explain(_event())
        assert result.degraded is True
        assert "sk-test-key" not in result.reason
        assert "401" in result.reason

    def test_should_merge_extra_headers(self) -> None:
        """附加请求头要合并进去。"""
        config = {
            "llm": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-test-key",
                "extra_headers": {"X-Gateway": "sentinel"},
            }
        }
        session = _ok_session()
        client = OpenAICompatibleClient(session=session, config=config)
        client.explain(_event())
        assert session.calls[0]["headers"]["X-Gateway"] == "sentinel"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class TestRequestPayload:
    """发出去的 JSON 长什么样。"""

    def test_should_send_expected_payload(self) -> None:
        """模型名、消息、温度、token 上限都要对。"""
        session = _ok_session()
        client = _client(session=session)
        client.explain(_event(thinking="我在清理临时目录"))
        body = session.calls[0]["json"]
        assert body["model"] == "deepseek-chat"
        assert body["stream"] is False
        assert body["temperature"] == 0
        assert body["max_tokens"] == 512
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        assert "清理临时目录" in body["messages"][1]["content"]

    def test_should_send_json_mode_by_default(self) -> None:
        """默认要求模型只吐 JSON。"""
        session = _ok_session()
        client = _client(session=session)
        client.explain(_event())
        body = session.calls[0]["json"]
        assert body["response_format"] == {"type": "json_object"}

    def test_json_mode_can_be_disabled(self) -> None:
        """有些网关不支持这个字段，要能关掉。"""
        session = _ok_session()
        client = _client(session=session, json_mode=False)
        client.explain(_event())
        assert "response_format" not in session.calls[0]["json"]

    def test_should_pass_timeout(self) -> None:
        """超时配置要透传。"""
        session = _ok_session()
        client = _client(session=session)
        client.explain(_event())
        assert session.calls[0]["timeout"] == 30


# ---------------------------------------------------------------------------
# 返回结构
# ---------------------------------------------------------------------------
class TestResponseParsing:
    """解析回来的结构。"""

    def test_should_return_same_structure_as_ollama(self) -> None:
        """**关键契约**：与本地 Ollama 返回的结构完全一致。"""
        session = _ok_session()
        api_result = _client(session=session).explain(_event())

        ollama_session = FakeSession(
            FakeResponse(200, {"message": {"content": MODEL_JSON}})
        )
        ollama_config = {
            "llm": {
                "enabled": True,
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
            }
        }
        ollama_result = OllamaClient(
            session=ollama_session, config=ollama_config
        ).explain(_event())

        assert isinstance(api_result, Explanation)
        assert api_result.detail.keys() == ollama_result.detail.keys()
        assert api_result.text == ollama_result.text
        assert api_result.degraded is ollama_result.degraded is False
        assert api_result.detail["title"] == ollama_result.detail["title"]
        assert api_result.detail["why"] == ollama_result.detail["why"]
        assert api_result.detail["touch"] == ollama_result.detail["touch"]
        assert api_result.detail["outcome"] == ollama_result.detail["outcome"]

    def test_should_expose_provider_name(self) -> None:
        """结果里要能看出是哪个提供方。"""
        result = _client().explain(_event())
        assert result.provider == "openai_compatible"
        assert result.model == "deepseek-chat"

    def test_should_render_human_readable_text(self) -> None:
        """对外仍然是一段人话。"""
        result = _client().explain(_event())
        assert "帮你把整个临时目录删掉" in result.text
        assert "原因：" in result.text
        assert "影响：" in result.text
        assert "后果：" in result.text

    def test_should_fallback_to_reasoning_content(self) -> None:
        """推理模型把正文放在 reasoning_content 时也要能用。"""
        session = FakeSession(
            FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": MODEL_JSON,
                            }
                        }
                    ]
                },
            )
        )
        result = _client(session=session).explain(_event())
        assert result.degraded is False
        assert "帮你把整个临时目录删掉" in result.text

    def test_should_fallback_to_legacy_text_field(self) -> None:
        """旧式补全形态 choices[0].text 也要认。"""
        session = FakeSession(
            FakeResponse(200, {"choices": [{"text": MODEL_JSON}]})
        )
        result = _client(session=session).explain(_event())
        assert result.degraded is False


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------
class TestDegradation:
    """失败必须降级，不能抛异常。"""

    def test_should_degrade_on_http_error(self) -> None:
        """HTTP 4xx/5xx 一律降级。"""
        session = FakeSession(FakeResponse(500, {}, text="boom"))
        result = _client(session=session).explain(_event())
        assert result.degraded is True
        assert "500" in result.reason
        assert "原始命令" in result.text

    def test_should_degrade_on_401(self) -> None:
        """鉴权失败要说人话。"""
        session = FakeSession(FakeResponse(401, {}, text="unauthorized"))
        result = _client(session=session).explain(_event())
        assert result.degraded is True
        assert "鉴权失败" in result.reason

    def test_should_degrade_on_connection_error(self) -> None:
        """连不上也要降级。"""
        session = FakeSession(error=requests.ConnectionError("无法连接"))
        result = _client(session=session).explain(_event())
        assert result.degraded is True
        assert "ConnectionError" in result.reason

    def test_should_degrade_on_timeout(self) -> None:
        """超时也要降级。"""
        session = FakeSession(error=requests.Timeout("timed out"))
        result = _client(session=session).explain(_event())
        assert result.degraded is True

    def test_should_degrade_on_non_json_response(self) -> None:
        """返回的不是 JSON 也要降级。"""
        class BadResponse(FakeResponse):
            def json(self):
                raise ValueError("不是 JSON")

        session = FakeSession(BadResponse(200))
        result = _client(session=session).explain(_event())
        assert result.degraded is True

    def test_should_degrade_on_empty_choices(self) -> None:
        """没有 choices 也要降级。"""
        session = FakeSession(FakeResponse(200, {"choices": []}))
        result = _client(session=session).explain(_event())
        assert result.degraded is True
        assert "没有可用的模型输出" in result.reason

    def test_should_degrade_on_unparseable_output(self) -> None:
        """模型吐出非 JSON 垃圾，走标注式兜底；兜不住就降级。"""
        session = FakeSession(
            FakeResponse(
                200,
                {"choices": [{"message": {"content": "我不知道"}}]},
            )
        )
        result = _client(session=session).explain(_event())
        assert result.degraded is True

    def test_should_degrade_when_disabled(self) -> None:
        """enabled=false 时不联网。"""
        session = _ok_session()
        client = _client(session=session, enabled=False)
        result = client.explain(_event())
        assert result.degraded is True
        assert session.calls == []

    def test_should_degrade_when_base_url_missing(self) -> None:
        """没配 base_url 就不该发请求。"""
        session = _ok_session()
        config = {"llm": {"enabled": True, "model": "x"}}
        client = OpenAICompatibleClient(session=session, config=config)
        result = client.explain(_event())
        assert result.degraded is True
        assert "base_url" in result.reason
        assert session.calls == []

    def test_should_degrade_when_model_missing(self) -> None:
        """没配模型名也不该发请求。"""
        session = _ok_session()
        config = {
            "llm": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
            }
        }
        client = OpenAICompatibleClient(session=session, config=config)
        result = client.explain(_event())
        assert result.degraded is True
        assert "模型名" in result.reason
        assert session.calls == []


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
class TestRedaction:
    """送出去之前必须脱敏（外部 API 尤其重要）。"""

    def test_should_redact_secret_before_sending(self) -> None:
        """命令里的密钥不能原样发出去。"""
        session = _ok_session()
        client = _client(session=session)
        client.explain(
            _event(command="curl -H 'Authorization: Bearer sk-live-abc123' http://x")
        )
        sent = session.calls[0]["json"]["messages"][1]["content"]
        assert "sk-live-abc123" not in sent


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    """熔断器对外部 API 同样生效。"""

    def test_should_open_breaker_after_repeated_failures(self) -> None:
        """连续失败达阈值后不再发请求。"""
        session = FakeSession(FakeResponse(500, {}, text="boom"))
        client = _client(session=session, failure_threshold=3)
        for _ in range(3):
            client.explain(_event())
        calls_before = len(session.calls)
        result = client.explain(_event())
        assert result.degraded is True
        assert "熔断" in result.reason
        assert len(session.calls) == calls_before


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
class TestFactory:
    """按配置挑提供方。"""

    def test_should_default_to_ollama(self) -> None:
        """没写 provider 时用本地 Ollama。"""
        config = {"llm": {"base_url": "http://127.0.0.1:11434"}}
        provider = build_provider(config)
        assert isinstance(provider, OllamaClient)
        assert provider.name == "ollama"

    def test_should_select_openai_compatible(self) -> None:
        """写 openai_compatible 就用 API 客户端。"""
        config = {
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            }
        }
        provider = build_provider(config)
        assert isinstance(provider, OpenAICompatibleClient)
        assert provider.name == "openai_compatible"

    def test_should_reject_unknown_provider(self) -> None:
        """拼错的名字要报错，不能静默回落。"""
        config = {"llm": {"provider": "openai_compatable"}}
        try:
            build_provider(config)
        except ValueError as exc:
            assert "openai_compatable" in str(exc)
        else:
            raise AssertionError("未知 provider 应当抛 ValueError")

    def test_normalize_aliases(self) -> None:
        """常见别名要能认。"""
        assert normalize_provider("Ollama") == "ollama"
        assert normalize_provider("openai") == "openai_compatible"
        assert normalize_provider("openai-compatible") == "openai_compatible"
        assert normalize_provider(None) == "ollama"

    def test_should_raise_api_error_on_bad_status(self) -> None:
        """chat() 直接调用时抛的是 ApiError（不是裸 HTTPError）。"""
        session = FakeSession(FakeResponse(429, {}, text="rate limited"))
        client = _client(session=session)
        try:
            client.chat("system", "user")
        except ApiError as exc:
            assert "限流" in str(exc)
        else:
            raise AssertionError("应当抛出 ApiError")


# ---------------------------------------------------------------------------
# 端到端（Mock）
# ---------------------------------------------------------------------------
class TestEndToEnd:
    """从事件到告警文案的完整链路。"""

    def test_explain_event_returns_one_paragraph(self) -> None:
        """对外入口返回一段话。"""
        text = _client().explain_event(_event())
        assert isinstance(text, str)
        assert "帮你把整个临时目录删掉" in text
        assert "。" in text

    def test_should_not_touch_real_network(self) -> None:
        """本文件的所有用例都不许碰真实网络。"""
        assert os.environ.get("SENTINEL_TEST_OFFLINE", "1") == "1"
