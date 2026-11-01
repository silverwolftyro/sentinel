"""L2 本地模型层 —— Ollama 客户端。

定位
====
哨兵的 L2 只做一件事：**把 L1 已经判定的危险事件，翻译成一句人话**。
它**不判风险**、**不建议放行或终止**（见 ``prompts.SENTINEL_EXPLAIN_TEMPLATE``）。

与 ``src/llm/base.py`` 的关系
=============================
v0.6.0 起，本类只负责**「怎么把对话发给本地 Ollama」**这一件事；
脱敏、Prompt 拼接、容错解析、渲染、降级全部由基类
:class:`~src.llm.base.LLMProvider` 统一处理 ——
这样接外部 API（:mod:`src.llm.api_client`）时不必重复实现降级契约。

三条硬约束
==========
1. **永不抛异常** —— 模型没开、超时、连不上、输出解析不出来，
   一律返回**降级结果**（``degraded=True``），由调用方落到
   「原始命令 + 规则原因」的兜底文案。
   这条契约抄自旧项目 ``core/llm_review.py:381-435``：
   「**缺失绝不等同于安全**」—— 模型说「安全」却没给理由时，
   连「安全」都要被改写掉。
2. **只允许本地端点** —— 哨兵承诺「所有推理在本地完成」。
   非回环地址的 ``base_url`` 会被**拒绝调用**（不抛异常，走降级）。
   需要接外部模型时，请显式改用 ``llm.provider: openai_compatible``，
   那条路径由 :mod:`src.llm.api_client` 负责，语义完全不同。
3. **不引入重型框架** —— 旧项目用 ``pydantic-ai``，但它自己的实测记录
   （``core/llm_review.py:206-218``，2026-10-22，本地 Ollama + qwen2.5:7b）
   显示 ``output_type=<BaseModel>`` **10 次失败 6 次（60%）**。
   哨兵只用标准 HTTP 调用 + 容错解析阶梯（``src/llm/parsing.py``）。

熔断器
======
连续失败达到阈值就**打开熔断**，在一段时间内直接跳过 L2，
避免日志洪峰把本地模型压垮（抄自 ``dsh-plain-approval/src/ollama-guard.js``）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from ..config_loader import load_config
from .base import CircuitBreaker, Explanation, LLMProvider, ProviderError

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "CircuitBreaker",
    "Explanation",
    "OllamaClient",
    "OllamaError",
]


class OllamaError(ProviderError):
    """本地模型调用失败（配置问题或服务不可用）。"""


#: 默认端点。
DEFAULT_BASE_URL = "http://localhost:11434"

#: 默认模型。
DEFAULT_MODEL = "qwen2.5:7b"

#: 默认超时（秒）。抄自旧项目 ``core/llm_review.py:77`` 的 20 秒。
DEFAULT_TIMEOUT = 20.0

#: 回环主机名。
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _is_loopback(host: str) -> bool:
    """判断主机是否是本机回环地址。"""
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaClient(LLMProvider):
    """本地 Ollama 客户端。

    :param base_url:   Ollama 地址，默认 ``http://localhost:11434``。
    :param model:      模型名，默认 ``qwen2.5:7b``。
    :param timeout:    单次调用超时（秒），默认 20。
    :param enabled:    是否启用；``False`` 时所有调用直接走降级。
    :param keep_alive: 模型在内存中的驻留时间；``0`` 表示用完即卸载（省显存）。
    :param session:    注入的 ``requests.Session``（测试用）。
    :param config:     已加载的配置字典；为 ``None`` 时自行读取。
    """

    #: 提供方标识。
    name = "ollama"

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
        keep_alive: Optional[int] = None,
        session: Optional[requests.Session] = None,
        *,
        config: Optional[Dict[str, Any]] = None,
        failure_threshold: int = 3,
        breaker_open_seconds: float = 300.0,
    ) -> None:
        if config is None:
            config = load_config()
        llm_config = (config.get("llm") or {}) if isinstance(config, dict) else {}

        self.base_url = str(
            base_url or llm_config.get("base_url") or DEFAULT_BASE_URL
        ).rstrip("/")
        # keep_alive 允许数字（秒）或 Ollama 的时长串（"5m" / "0"），
        # 原样透传给服务端，不要强制转 int。
        self.keep_alive = (
            keep_alive
            if keep_alive is not None
            else llm_config.get("keep_alive", 0)
        )
        self.session = session or requests.Session()

        super().__init__(
            model=str(model or llm_config.get("model") or DEFAULT_MODEL),
            timeout=float(
                timeout
                if timeout is not None
                else llm_config.get("timeout", DEFAULT_TIMEOUT)
            ),
            enabled=bool(
                enabled
                if enabled is not None
                else llm_config.get("enabled", True)
            ),
            redact_before_llm=bool(
                llm_config.get("redact_before_llm", True)
            ),
            max_context_chars=int(llm_config.get("max_context_chars", 4000)),
            failure_threshold=int(
                llm_config.get("failure_threshold", failure_threshold)
            ),
            breaker_open_seconds=float(
                llm_config.get("breaker_open_seconds", breaker_open_seconds)
            ),
        )

        # 红线：哨兵承诺「所有推理在本地完成」。非回环端点一律拒绝调用。
        self.local_only = _is_loopback(self._host())

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def blocked_reason(self) -> str:
        """非回环端点一律拒绝调用（红线 R4「本地闭环」）。"""
        if self.local_only:
            return ""
        return (
            f"模型端点不是本机地址（{self.base_url}），"
            "哨兵只允许本地推理，已拒绝调用"
        )

    def chat(self, system: str, user: str) -> str:
        """调用本地模型，返回原始文本。"""
        return self._chat(system, user)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _host(self) -> str:
        """从 base_url 里取出主机名。"""
        from urllib.parse import urlparse

        return urlparse(self.base_url).hostname or ""

    def _chat(self, system: str, user: str) -> str:
        """调用本地模型，返回原始文本。

        优先走 Ollama 原生 ``/api/chat``（支持 ``format: "json"`` 语法约束）；
        若服务端不支持（404），退回 OpenAI 兼容端点 ``/v1/chat/completions``。
        """
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_predict": 512},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        response = self.session.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 404:
            # ⚠️ 404 有两种含义，必须区分，否则会把「模型没装」误当成
            #    「端点不支持」而错误地退回兼容端点：
            #      ① 模型不存在 —— Ollama 返回 {"error":"model 'x' not found"}
            #      ② 端点不存在 —— 极老的服务端才有
            body = ""
            try:
                body = (response.text or "").lower()
            except Exception:  # noqa: BLE001 —— 取不到正文就按 ② 处理
                body = ""
            if "model" in body or self.model.lower() in body:
                raise OllamaError(
                    f"本地模型不存在：{self.model}"
                    f"（请先执行：ollama pull {self.model}）"
                )
            return self._chat_openai_compatible(system, user)
        response.raise_for_status()
        data = response.json()
        content = str(data.get("message", {}).get("content") or "")
        if not content:
            content = str(data.get("response") or "")
        return content

    def _chat_openai_compatible(self, system: str, user: str) -> str:
        """OpenAI 兼容端点兜底。"""
        payload = {
            "model": self.model,
            "stream": False,
            "temperature": 0,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        response = self.session.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content") or "")
