"""v3.2.1 —— 本地 NInfer（Bonsai2-27B）适配测试。

为什么有这一组
==============

v3.2.1 的全部改动都来自**一次真实探测**
（``_构建与发布/scripts/ninfer_probe.py``），
实测踩到的坑逐条钉在这里，防止以后改回去：

1. **单并发** —— NInfer 是 ``max-concurrency=1``，
   同时发两个请求**引擎直接崩** → 必须客户端排队；
2. **超时必须 600 秒** —— 27B 推理模型一次「思考」就吃掉几千 token；
3. **``response_format`` 不被支持** —— 发过去直接 400；
4. **正文可能在 ``reasoning_content`` 里** ——
   ``content`` 经常是空的，思考过程**绝不能**进看板文案；
5. **本机引擎不该被喊「数据会离开本机」** —— 那是假话。

**全部离线** —— 用假的 ``requests.Session`` 拦截请求。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List

import pytest
import requests

from src.llm.api_client import (
    LOCAL_ENGINE_MIN_TIMEOUT,
    ApiError,
    OpenAICompatibleClient,
    is_local_endpoint,
)
from src.llm.base import engine_lock_for
from src.llm.parsing import extract_last_json_object, strip_thinking

#: 本机 NInfer 的地址。
NINFER_URL = "http://127.0.0.1:8888/v1"

#: NInfer 真实返回的形状（v3.2.1 实机抓的）。
NINFER_REASONING = (
    "我们需要回答用户：要求输出 JSON，字段 title, why, touch, outcome。"
    "任务：给完全不懂技术普通人解释 AI Agent 刚刚做的一件事。"
    "关键：Agent 说准备写单元测试，但命令在删除构建缓存，明显不一致。"
    '{"title":"它说要写单元测试，却在删除构建缓存",'
    '"why":"删除缓存的原因无法确定。",'
    '"touch":"项目里的构建缓存目录（会删除）",'
    '"outcome":"缓存被删掉后无法恢复。"}'
)


# ---------------------------------------------------------------------------
# 假会话
# ---------------------------------------------------------------------------
class FakeResponse:
    """模拟 ``requests.Response``。"""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self) -> None:
        """模拟状态码检查。"""
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP {}".format(self.status_code))

    def json(self) -> Any:
        """返回响应体。"""
        return self._payload


class FakeSession:
    """模拟 ``requests.Session``：按顺序返回预设响应。"""

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def post(self, url, json=None, headers=None, timeout=None):
        """记录调用并返回下一个预设响应。"""
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            self.calls.append(
                {
                    "url": url,
                    # ⚠️ 必须存**拷贝** —— 生产代码在重试时会原地
                    # ``pop("response_format")``，存引用会前后串味。
                    "json": dict(json) if isinstance(json, dict) else json,
                    "headers": headers,
                    "timeout": timeout,
                }
            )
            item = self.responses.pop(0) if self.responses else FakeResponse()
            if isinstance(item, Exception):
                raise item
            return item
        finally:
            with self.lock:
                self.active -= 1


def _ninf_config(**extra: Any) -> Dict[str, Any]:
    """造一份指向本机 NInfer 的配置。

    :param extra: 要覆盖的字段。
    :return: 配置字典。
    """
    llm = {
        "provider": "openai_compatible",
        "base_url": NINFER_URL,
        "model": "bonsai2-27b",
        "api_key": "sk-no-key-required",
        "timeout": 600,
        "max_tokens": 8192,
        "json_mode": False,
        "enabled": True,
        "redact_before_llm": True,
    }
    llm.update(extra)
    return {"llm": llm}


# ---------------------------------------------------------------------------
# 1. 本机识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8888/v1",
        "http://localhost:11434",
        "http://0.0.0.0:8000/v1",
        "http://[::1]:8888/v1",
        "http://ninfer.local:8888/v1",
    ],
)
def test_local_endpoints_are_recognized(url: str) -> None:
    """本机地址必须被认出来 —— 否则会对本地引擎喊「数据要外送」。"""
    assert is_local_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com/v1",
        "https://api.moonshot.cn/v1",
        "http://192.168.1.10:8888/v1",
        "",
        "不是一个地址",
    ],
)
def test_remote_endpoints_are_not_local(url: str) -> None:
    """公网地址不是本机 —— 红线 R4 的边界不能搞错。"""
    assert is_local_endpoint(url) is False


# ---------------------------------------------------------------------------
# 2. 超时下限
# ---------------------------------------------------------------------------
def test_local_engine_timeout_floor_is_600() -> None:
    """本机引擎的超时**不得低于 600 秒** —— 60 秒必然判不了。"""
    client = OpenAICompatibleClient(config=_ninf_config(timeout=20))
    assert client.timeout >= LOCAL_ENGINE_MIN_TIMEOUT
    assert client.timeout == 600.0


def test_remote_engine_keeps_configured_timeout() -> None:
    """公网 API 的超时按配置来 —— 不能被本地引擎的下限波及。"""
    client = OpenAICompatibleClient(
        config={
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "x",
                "timeout": 45,
            }
        }
    )
    assert client.timeout == 45.0


def test_explicit_timeout_argument_still_wins_for_remote() -> None:
    """显式传入的超时优先（远程场景）。"""
    client = OpenAICompatibleClient(
        timeout=33,
        config={
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "x",
                "timeout": 45,
            }
        },
    )
    assert client.timeout == 33.0


# ---------------------------------------------------------------------------
# 3. 串行锁
# ---------------------------------------------------------------------------
def test_same_engine_shares_one_lock() -> None:
    """同一台引擎只能有一把锁 —— 否则并发照样发生。"""
    first = engine_lock_for(NINFER_URL)
    second = engine_lock_for(NINFER_URL + "/")
    assert first is second


def test_different_engines_use_different_locks() -> None:
    """本地引擎和云端 API 各排各的队 —— 云端不被本地堵住。"""
    local = engine_lock_for(NINFER_URL)
    remote = engine_lock_for("https://api.deepseek.com/v1")
    assert local is not remote


def test_concurrent_calls_are_serialized() -> None:
    """两个线程同时调同一台引擎 → **绝不重叠**。

    NInfer 是单并发，重叠就崩。
    """
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "choices": [
                        {"message": {"content": '{"title":"甲","why":"乙"}'}}
                    ]
                }
            ),
            FakeResponse(
                payload={
                    "choices": [
                        {"message": {"content": '{"title":"丙","why":"丁"}'}}
                    ]
                }
            ),
        ]
    )
    client = OpenAICompatibleClient(config=_ninf_config(), session=session)

    def _work() -> None:
        client.chat("system", "user")

    threads = [threading.Thread(target=_work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(session.calls) == 2
    assert session.max_active == 1


# ---------------------------------------------------------------------------
# 4. 思考过程剥离
# ---------------------------------------------------------------------------
def test_strip_thinking_removes_tagged_block() -> None:
    """带标签的思考段要整段删掉。"""
    text = "<think>我得先想想…</think>最终答案"
    assert strip_thinking(text) == "最终答案"


def test_strip_thinking_removes_unterminated_block() -> None:
    """被 max_tokens 截断的思考段（没闭合）也要删掉。"""
    text = "<think>我得先想想，结果预算不够了"
    assert strip_thinking(text) == ""


def test_strip_thinking_keeps_plain_text() -> None:
    """普通文本不能被误伤。"""
    assert strip_thinking("就是一句话") == "就是一句话"


def test_extract_last_json_object_picks_the_last() -> None:
    """推理模型先聊后答 —— 要取**最后**那段 JSON。"""
    text = '先看这个 {"a":1}\n然后给结论 {"title":"x","why":"y"}'
    assert extract_last_json_object(text) == '{"title":"x","why":"y"}'


def test_extract_last_json_object_none_when_absent() -> None:
    """没有 JSON 就老实说没有。"""
    assert extract_last_json_object("完全没有 JSON") is None


# ---------------------------------------------------------------------------
# 5. 内容提取
# ---------------------------------------------------------------------------
def test_content_wins_over_reasoning() -> None:
    """正文存在时用正文 —— 思考过程一律不碰。"""
    client = OpenAICompatibleClient(config=_ninf_config())
    data = {
        "choices": [
            {
                "message": {
                    "content": '{"title":"正文"}',
                    "reasoning_content": "一肚子思考",
                }
            }
        ]
    }
    assert client._extract_content(data) == '{"title":"正文"}'


def test_content_is_stripped_of_thinking_tags() -> None:
    """正文里混着 ``<think>`` 也要剥掉。"""
    client = OpenAICompatibleClient(config=_ninf_config())
    data = {
        "choices": [
            {"message": {"content": "<think>思考</think>{\"title\":\"正文\"}"}}
        ]
    }
    assert client._extract_content(data) == '{"title":"正文"}'


def test_json_is_recovered_from_reasoning_when_content_empty() -> None:
    """正文空、答案在 ``reasoning_content`` 里 → 把 JSON 抠出来。"""
    client = OpenAICompatibleClient(config=_ninf_config())
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": NINFER_REASONING,
                }
            }
        ]
    }
    result = client._extract_content(data)
    assert '"title"' in result
    assert "我们需要回答用户" not in result


def test_thinking_never_leaks_into_the_answer() -> None:
    """**思考过程一个字都不能进告警文案**。"""
    client = OpenAICompatibleClient(config=_ninf_config())
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": "我得先算一算，这事儿有点玄乎。",
                }
            }
        ]
    }
    assert client._extract_content(data) == ""


def test_text_fallback_is_stripped() -> None:
    """旧式补全形态（``choices[0].text``）也认。"""
    client = OpenAICompatibleClient(config=_ninf_config())
    data = {"choices": [{"text": "<think>思考</think>正文"}]}
    assert client._extract_content(data) == "正文"


# ---------------------------------------------------------------------------
# 6. response_format 退让
# ---------------------------------------------------------------------------
def test_response_format_400_triggers_retry_without_it() -> None:
    """引擎不认 ``response_format`` → 自动摘掉重试一次。"""
    session = FakeSession(
        [
            FakeResponse(
                status_code=400,
                text=(
                    '{"error":{"code":"response_format_not_supported",'
                    '"message":"structured output requires the server to '
                    'start with --structured-output"}}'
                ),
                payload={},
            ),
            FakeResponse(
                payload={
                    "choices": [{"message": {"content": '{"title":"好"}'}}]
                }
            ),
        ]
    )
    client = OpenAICompatibleClient(
        config=_ninf_config(json_mode=True), session=session
    )
    assert client.chat("s", "u") == '{"title":"好"}'
    assert len(session.calls) == 2
    # 第一次带 response_format，第二次不带。
    assert "response_format" in session.calls[0]["json"]
    assert "response_format" not in session.calls[1]["json"]
    # 记住这件事，后面不再白跑一次。
    assert client.json_mode is False


def test_other_400_is_not_retried() -> None:
    """别的 400（比如模型名不对）不该重试 —— 重试也没用。"""
    session = FakeSession(
        [FakeResponse(status_code=400, text='{"error":"model not found"}')]
    )
    client = OpenAICompatibleClient(config=_ninf_config(), session=session)
    with pytest.raises(ApiError):
        client.chat("s", "u")
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# 7. 数据流向的话术
# ---------------------------------------------------------------------------
def test_local_engine_does_not_warn_data_leaves_machine(caplog) -> None:
    """本机引擎**不能**喊「数据会离开本机」—— 那是假话。"""
    with caplog.at_level("WARNING"):
        OpenAICompatibleClient(config=_ninf_config())
    assert "离开本机" not in caplog.text


def test_remote_engine_warns_data_leaves_machine(caplog) -> None:
    """公网 API 必须提醒：内容会离开本机。"""
    with caplog.at_level("WARNING"):
        OpenAICompatibleClient(
            config={
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "x",
                }
            }
        )
    assert "离开本机" in caplog.text


# ---------------------------------------------------------------------------
# 8. 端到端
# ---------------------------------------------------------------------------
def test_explain_against_ninfer_shape() -> None:
    """用 NInfer 的真实响应形状跑一遍 ``explain``。

    这条是整个适配的**验收线**：最终落到看板上的那段话，
    既要**有内容**，又**不能带思考过程**。
    """
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": NINFER_REASONING,
                            }
                        }
                    ]
                }
            )
        ]
    )
    client = OpenAICompatibleClient(config=_ninf_config(), session=session)
    result = client.explain(
        {
            "thinking": "准备把解析器拆成两层，先写词法层的单元测试。",
            "tool": {"name": "Bash", "command": "rm -rf ./build/cache"},
            "verdict": {
                "verdict": "DANGEROUS",
                "rule_id": "dangerous_command",
                "level": "critical",
                "severity": "standard",
                "reason": "匹配到高危命令：递归强制删除",
                "evidence": "rm -rf ./build/cache",
            },
        }
    )
    assert result.degraded is False
    assert "它说要写单元测试" in result.text
    assert "我们需要回答用户" not in result.text
    assert "关键：Agent 说准备写单元测试" not in result.text


def test_explain_degrades_when_engine_is_down() -> None:
    """引擎连不上 → **降级**，不抛异常（红线 R5）。"""
    session = FakeSession([requests.ConnectionError("连不上")])
    client = OpenAICompatibleClient(config=_ninf_config(), session=session)
    result = client.explain(
        {
            "tool": {"name": "Bash", "command": "rm -rf ./build/cache"},
            "verdict": {"reason": "匹配到高危命令：递归强制删除"},
        }
    )
    assert result.degraded is True
    assert "rm -rf ./build/cache" in result.text


# ---------------------------------------------------------------------------
# 9. 配置与脚本
# ---------------------------------------------------------------------------
def test_example_config_points_at_ninfer() -> None:
    """配置模板必须指向本机 NInfer，且超时给到 600。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "config.example.yaml",
    )
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "127.0.0.1:8888" in text
    assert "bonsai2-27b" in text
    assert "sk-no-key-required" in text
    assert "timeout: 600" in text


def test_ninfer_probe_script_exists() -> None:
    """连接测试脚本必须在（它就是本次的验收工具）。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_构建与发布",
        "scripts",
        "ninfer_probe.py",
    )
    assert os.path.exists(path)


def test_night_crawl_has_boundary_watchdog() -> None:
    """演习脚本必须有越界看门狗（任务二的要求）。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_构建与发布",
        "scripts",
        "final_night_crawl.py",
    )
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "_start_boundary_watch" in text
    assert "boundary_violations" in text
    assert "Agent 没有越界" in text


# ---------------------------------------------------------------------------
# 10. 开箱体检必须探得到本机 NInfer
# ---------------------------------------------------------------------------
def test_health_check_probes_local_openai_engine(monkeypatch) -> None:
    """本机 OpenAI 兼容引擎**必须**被体检探到。

    v3.2.1 实机发现：旧体检只看 ``provider == "ollama"``，
    于是把本机 NInfer 当成云端**跳过了** ——
    报告说「跳过」，用户以为没问题，其实引擎可能根本没开。
    """
    from src.core import health

    class _Resp:
        """假响应。"""

        status_code = 200

        def json(self) -> Dict[str, Any]:
            """返回模型列表。"""
            return {"data": [{"id": "bonsai2-27b"}]}

    monkeypatch.setattr(
        "requests.get", lambda *a, **k: _Resp(), raising=False
    )
    item = health.check_ollama(
        {
            "llm": {
                "provider": "openai_compatible",
                "base_url": NINFER_URL,
                "model": "bonsai2-27b",
            }
        }
    )
    assert item.status == health.STATUS_OK
    assert "bonsai2-27b" in item.name


def test_health_check_skips_cloud_engine(monkeypatch) -> None:
    """云端 API 不该被当成本地模型去探。"""
    from src.core import health

    item = health.check_ollama(
        {
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            }
        }
    )
    assert item.status == health.STATUS_SKIP
