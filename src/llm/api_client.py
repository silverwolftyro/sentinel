"""L2 通用 API 接入 —— OpenAI 兼容接口客户端。

为什么要有它
============
v0.6.0 之前，L2 只能连本地 Ollama。但很多团队并没有本地 GPU，
或者想把更贵的推理交给外部大模型（DeepSeek、Kimi/Moonshot、
硅基流动、零一、以及任何自建的 vLLM / LM Studio / one-api 网关）。
这些服务**绝大多数**都提供 OpenAI 兼容的 ``/chat/completions`` 接口 ——
所以一个客户端就能覆盖它们。

与本地 Ollama 的**关键语义差异**
================================
============ ==========================================
              Ollama                         外部 API
============ ==========================================
数据流向      不出本机                        **会离开本机**
端点          ``/api/chat``                   ``/v1/chat/completions``
鉴权          无                              ``Authorization: Bearer``
JSON 约束     ``format: "json"``              ``response_format``
============ ==========================================

⚠️ **数据出本机这件事必须被显式承认**：哨兵送进模型的是
「Agent 的思考过程 + 命令原文 + 规则判定」。
启用本客户端 = 允许这些内容离开本机。

因此本模块做了三件事来把风险压到最低：

1. **必须显式配置**：只有把 ``llm.provider`` 改成
   ``openai_compatible`` 才会走到这里，默认仍是本地 Ollama；
2. **送模型前强制脱敏**（:func:`src.llm.redaction.redact_secrets`，
   由基类统一执行）：密钥、令牌、个人信息在**离开本机之前**就被抹掉；
3. **密钥不落盘、不进日志**：优先从环境变量 ``SENTINEL_LLM_API_KEY``
   读取，配置里写的 ``api_key`` 仅作为兜底；任何日志都不会打印密钥。

三条硬约束（与本地客户端一致）
==============================
1. **永不抛异常** —— 调用失败一律降级；
2. **统一返回结构** —— 返回的 :class:`~src.llm.base.Explanation`
   与 Ollama 客户端**完全一致**（标题 / 原因 / 影响 / 后果）；
3. **不引入 SDK** —— 只用 ``requests``，不装 ``openai`` 包。
   兼容接口足够简单，没必要为它引入一整套依赖树。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from ..config_loader import load_config
from .base import LLMProvider, ProviderError, engine_lock_for
from .parsing import extract_last_json_object, strip_thinking
from .redaction import redact_secrets

logger = logging.getLogger(__name__)

__all__ = [
    "API_KEY_ENV",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "LOCAL_ENGINE_MIN_TIMEOUT",
    "MIN_MAX_TOKENS",
    "PROVIDER_KEY_ENV",
    "ApiError",
    "OpenAICompatibleClient",
    "is_local_endpoint",
    "resolve_api_key",
]

#: 默认超时（秒）。
DEFAULT_TIMEOUT = 60.0

#: ``max_tokens`` 的**下限**（v3.9.0）。
#:
#: 为什么要有下限
#: =============
#:
#: 极短的输出请求（比如「只回答一个字」）过去会把 ``max_tokens``
#: 设成个位数（体检那关就是 ``4``）。可**推理模型**会把额度
#: 先花在思考段上 —— 4 个 token 全被思考吃掉，
#: 正文一个字都吐不出来。
#:
#: 结果就是：**密钥明明有效，体检却报失败**，
#: 而报错信息是「响应里没有可用的模型输出」——
#: 用户完全看不出是自己的额度设小了。
#:
#: 所以这里给一个**硬下限**：无论调用方写多小，都按 50 走。
#: 50 个 token 足够放下「思考段 + 一个字」，
#: 而多花的那点 token 对一次「你好」级别的请求毫厘无感。
MIN_MAX_TOKENS = 50

#: **本地引擎**的超时下限（秒）。
#:
#: v3.2.1 实机踩到：本机 NInfer 是 **27B 推理模型**，
#: 单次回复光「思考」就可能吃掉几千 token
#: （实测 44 token/秒，1024 token 的思考段要 23 秒），
#: 再加上首次预填充，60 秒的超时**必然**判不了。
#:
#: 所以：**只要引擎在本机**，超时下限就拉到 600 秒。
#: 超时是上限不是固定等待 —— 模型答完立刻就返回，不受影响。
LOCAL_ENGINE_MIN_TIMEOUT = 600.0

#: 默认模型名（留空由用户指定 —— 各家模型名不通用）。
DEFAULT_MODEL = ""

#: 本机地址 —— 命中这些就认为「数据不出本机」。
_LOCAL_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        # 「0.0.0.0」出现在 URL 里表示「本机」（Windows 上等价于 localhost）。
        "0.0.0.0",  # noqa: S104 —— 客户端要访问的主机名，不是监听地址
        "::1",
    }
)

#: 存放 API 密钥的环境变量名（**推荐**用这个，不要把密钥写进配置文件）。
API_KEY_ENV = "SENTINEL_LLM_API_KEY"

#: 各家服务**常见**的密钥环境变量（按 ``base_url`` 的主机名匹配）。
#:
#: 为什么要有这张表：用户机器上往往**早就**配好了
#: ``DEEPSEEK_API_KEY`` / ``MOONSHOT_API_KEY`` 之类，
#: 让他再单独配一个 ``SENTINEL_LLM_API_KEY`` 纯属多余负担。
#:
#: ⚠️ 优先级上通用名仍然**压过**专用名 —— ``SENTINEL_LLM_API_KEY``
#: 永远排第一，用户想强制指定时用它。
PROVIDER_KEY_ENV: Dict[str, str] = {
    "api.deepseek.com": "DEEPSEEK_API_KEY",
    "api.moonshot.cn": "MOONSHOT_API_KEY",
    "api.kimi.com": "KIMI_API_KEY",
    "open.bigmodel.cn": "ZHIPU_API_KEY",
    "api.siliconflow.cn": "SILICONFLOW_API_KEY",
    "dashscope.aliyuncs.com": "DASHSCOPE_API_KEY",
    "openrouter.ai": "OPENROUTER_API_KEY",
    "api.openai.com": "OPENAI_API_KEY",
}

#: 默认端点前缀（OpenAI 兼容协议的标准路径）。
_OPENAI_PATH = "/v1/chat/completions"


def is_local_endpoint(base_url: str) -> bool:
    """判断这个地址是不是**本机**的引擎。

    本机 = 数据不出本机 —— 红线 R4 的默认姿态。
    本机引擎（Ollama / NInfer / 本地 vLLM）都不需要「数据会离开本机」
    那句警告，反过来还会误导用户。

    :param base_url: API 根地址。
    :return: 是本机地址返回 ``True``。
    """
    try:
        host = (urlparse(str(base_url or "")).hostname or "").lower()
    except ValueError:  # noqa: BLE001 —— 地址不合法就当不是本机
        return False
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    # 同机局域网名（NInfer 常这样部署：``http://ninfer.local:8888/v1``）
    return host.endswith(".local")


def resolve_api_key(
    explicit: Optional[str] = None,
    base_url: str = "",
    configured: str = "",
) -> str:
    """按优先级解析 API 密钥。

    优先级（从高到低）：

    1. 显式传入的参数；
    2. 通用环境变量 ``SENTINEL_LLM_API_KEY``；
    3. **配置文件里显式写下的** ``api_key``；
    4. **按主机名自动发现**的厂商专用环境变量
       （``DEEPSEEK_API_KEY`` / ``MOONSHOT_API_KEY`` …）。

    ⚠️ 第 3 条排在第 4 条**前面**，是刻意的：
    用户**亲手**写进配置文件的密钥，代表「我就用这个」，
    不能被一个「碰巧也在机器上存在」的环境变量顶掉。
    而第 4 条只是**便利**：用户机器上早就配好了
    ``DEEPSEEK_API_KEY``，让他再单独配一个 ``SENTINEL_LLM_API_KEY``
    纯属多余负担 —— 配置文件里没写密钥时它就该自动生效。

    :param explicit: 显式传入的密钥。
    :param base_url: API 根地址，用来推断该用哪家的环境变量。
    :param configured: 配置文件里的密钥。
    :return: 解析出的密钥；没有则返回空串。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    generic = os.environ.get(API_KEY_ENV, "")
    if generic.strip():
        return generic.strip()
    if configured and configured.strip():
        return configured.strip()
    host = ""
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:  # noqa: BLE001 —— 地址不合法就当没匹配上
        host = ""
    env_name = PROVIDER_KEY_ENV.get(host, "")
    if env_name:
        specific = os.environ.get(env_name, "")
        if specific.strip():
            return specific.strip()
    return ""


class ApiError(ProviderError):
    """外部 API 调用失败（鉴权失败、限流、模型不存在等）。"""


class OpenAICompatibleClient(LLMProvider):
    """OpenAI 兼容 API 客户端。

    覆盖 DeepSeek、Kimi（Moonshot）、智谱、硅基流动、阿里百炼、
    OpenRouter，以及任何自建的 one-api / new-api / vLLM / LM Studio 网关。

    :param base_url:   API 根地址，例如
        ``https://api.deepseek.com/v1``。
        末尾带不带 ``/v1`` 都行 —— 客户端会自动补全。
    :param api_key:    API 密钥；留空则从环境变量
        ``SENTINEL_LLM_API_KEY`` 读取。
    :param model:      模型名，例如 ``deepseek-chat``。
    :param timeout:    单次调用超时（秒），默认 60（外部网络比本地慢）。
    :param enabled:    是否启用。
    :param temperature: 采样温度；默认 0（要可复现的解释，不要发挥）。
    :param max_tokens: 单次回复的最大 token 数。
    :param json_mode:  是否发送 ``response_format={"type":"json_object"}``。
    :param extra_headers: 附加请求头（例如某些网关的自定义头）。
    :param session:    注入的 ``requests.Session``（测试用）。
    :param config:     已加载的配置字典；为 ``None`` 时自行读取。
    """

    #: 提供方标识。
    name = "openai_compatible"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
        timeout_floor: Optional[float] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        session: Optional[requests.Session] = None,
        *,
        config: Optional[Dict[str, Any]] = None,
        failure_threshold: int = 3,
        breaker_open_seconds: float = 300.0,
        short_output: bool = False,
    ) -> None:
        if config is None:
            config = load_config()
        llm_config = (config.get("llm") or {}) if isinstance(config, dict) else {}

        self.base_url = str(
            base_url or llm_config.get("base_url") or ""
        ).strip().rstrip("/")
        # 密钥优先级：显式参数 > 通用环境变量 > 厂商专用环境变量 > 配置项。
        # 环境变量排在配置项前面，是为了让「不落盘」成为默认做法。
        self.api_key = resolve_api_key(
            api_key,
            self.base_url,
            str(llm_config.get("api_key") or ""),
        )

        self.temperature = float(
            temperature
            if temperature is not None
            else llm_config.get("temperature", 0)
        )
        self.max_tokens = max(
            MIN_MAX_TOKENS,
            int(
                max_tokens
                if max_tokens is not None
                else llm_config.get("max_tokens", 512)
            ),
        )
        self.json_mode = bool(
            json_mode if json_mode is not None else llm_config.get("json_mode", True)
        )
        #: v3.5.0：思考强度（``none`` / ``low`` / ``medium`` / ``high``）。
        #: 留空 = 不发这个字段（服务端自己决定）。
        self.reasoning_effort = str(
            reasoning_effort
            if reasoning_effort is not None
            else llm_config.get("reasoning_effort", "") or ""
        ).strip()
        #: v3.9.0：这是不是一次**极短输出**请求。
        self.short_output = bool(short_output)
        if self.short_output:
            # ⚠️ v3.9.0：**强制**关掉推理 —— 不看配置里写的是什么。
            #
            # 极短输出请求（只要求回答一个字）最怕的就是推理模型：
            # 思考段把额度吃光，正文一个字都吐不出来，
            # 于是「密钥有效」被误判成「密钥无效」。
            # 这里直接覆盖配置，保证 Payload 里**一定**带
            # ``reasoning_effort: "none"``。
            self.reasoning_effort = "none"
            # ⚠️ 同理，**强制关掉 JSON 模式** ——
            # 「只回答一个字」和「必须输出 JSON 对象」是矛盾的，
            # 而且 OpenAI 兼容网关在 ``response_format=json_object``
            # 下会要求提示词里出现 "json" 字样，否则直接 400。
            self.json_mode = False
        headers = llm_config.get("extra_headers") or {}
        self.extra_headers: Dict[str, str] = {
            str(key): str(value)
            for key, value in dict(headers).items()
        }
        if extra_headers:
            self.extra_headers.update(
                {str(k): str(v) for k, v in extra_headers.items()}
            )

        self.session = session or requests.Session()

        #: 引擎是不是**本机**的（决定超时下限与那句警告）。
        self.is_local = is_local_endpoint(self.base_url)

        # ⚠️ **本机引擎的超时下限** ——
        # 27B 推理模型一次思考就能吃掉几千 token（实测 44 token/秒），
        # 60 秒的超时会让每一次判定都必然失败。
        # 超时是**上限**不是固定等待：模型答完立刻返回。
        raw_timeout = float(
            timeout
            if timeout is not None
            else llm_config.get("timeout", DEFAULT_TIMEOUT)
        )
        if self.is_local:
            # ⚠️ v3.5.0：``timeout_floor`` 是**显式覆盖** ——
            #    L2 极速过滤要的就是「10 秒就放弃」，
            #    那条 600 秒的下限是给「等 27B 慢慢思考」准备的，
            #    对极速过滤来说太长了（会堵住整个队列）。
            floor = (
                float(timeout_floor)
                if timeout_floor is not None
                else float(
                    llm_config.get("timeout_floor", LOCAL_ENGINE_MIN_TIMEOUT)
                )
            )
            raw_timeout = max(raw_timeout, floor)

        super().__init__(
            model=str(model or llm_config.get("model") or DEFAULT_MODEL),
            timeout=raw_timeout,
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

        if self.enabled and self.base_url:
            self._warn_data_leaves_machine()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def blocked_reason(self) -> str:
        """配置不全时拒绝调用（而不是发一个注定失败的请求）。"""
        if not self.base_url:
            return "未配置 base_url，无法调用外部模型 API"
        if not self.model:
            return "未配置模型名（llm.model），无法调用外部模型 API"
        return ""

    def endpoint(self) -> str:
        """算出最终的请求地址。

        兼容三种写法，用户怎么填都能用：

        * ``https://api.deepseek.com`` → 补成 ``/v1/chat/completions``
        * ``https://api.deepseek.com/v1`` → 补成 ``/chat/completions``
        * ``https://.../v1/chat/completions`` → 原样使用
        """
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}{_OPENAI_PATH}"

    def request_headers(self) -> Dict[str, str]:
        """组装请求头（**绝不包含密钥的明文日志**）。"""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "sentinel-auditor/0.6",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def chat(self, system: str, user: str) -> str:
        """调用 OpenAI 兼容接口，返回原始文本。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: 模型的原始输出（可能是脏 JSON，由基类负责解析）。
        :raises ApiError: 请求失败或响应结构异常。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "temperature": self.temperature,
            # ⚠️ v3.9.0：这里**再兜一次**下限 ——
            #    ``self.max_tokens`` 在构造时已经抬到 50 以上，
            #    但它是公开属性 —— 运行期被改小的话，这里也要挡住。
            "max_tokens": max(MIN_MAX_TOKENS, int(self.max_tokens)),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.json_mode:
            # 不是所有网关都支持这个字段，但支持的服务端会因此
            # 少很多「不是 JSON」的输出 —— 值得发。
            payload["response_format"] = {"type": "json_object"}
        if self.short_output:
            # ⚠️ v3.9.0：极短输出请求**强制**注入 ——
            #    哪怕运行期有人把 ``reasoning_effort`` 改成了别的，
            #    这里也照发 "none"。
            payload["reasoning_effort"] = "none"
        elif self.reasoning_effort:
            # ⚠️ v3.5.0：**关掉思考** ——
            #    L2 是「极速过滤」，不需要 CoT；
            #    思考段会把单次判定拖长好几倍，
            #    而本地引擎是**单并发**的，堵一次就是堵全部。
            #    ⚠️ 不支持的网关会 400 —— 那时它会走「摘字段重试」的老路。
            payload["reasoning_effort"] = self.reasoning_effort

        url = self.endpoint()

        # ⚠️ **同一台引擎必须串行** ——
        # NInfer 是**单并发**（``max-concurrency=1``），
        # 同时发两个请求，引擎会**直接崩**（连接断掉，错误信息毫无意义）。
        # 哨兵天生多线程：后台解释线程、漂移判定线程、晨报线程
        # 都可能同一时刻要模型 —— 所以排队必须发生在**客户端**。
        with engine_lock_for(self.base_url):
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    headers=self.request_headers(),
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001 —— 统一转成 ApiError
                raise ApiError(f"{type(exc).__name__}: {exc}") from exc

            status = int(getattr(response, "status_code", 0) or 0)
            if status == 400:
                # ⚠️ 有些引擎**不认**这两个可选字段：
                #    ``response_format``（NInfer 默认不开结构化输出）
                #    与 ``reasoning_effort``（v3.5.0 新增）。
                #    退一步：把不认的那个摘掉再试一次，并且**记住** ——
                #    以后都不发了，省一次注定失败的请求。
                dropped = self._drop_unsupported_fields(payload, response)
                if dropped:
                    try:
                        response = self.session.post(
                            url,
                            json=payload,
                            headers=self.request_headers(),
                            timeout=self.timeout,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise ApiError(f"{type(exc).__name__}: {exc}") from exc
                    status = int(getattr(response, "status_code", 0) or 0)

            if status >= 400:
                raise ApiError(self._http_error_text(status, response))

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001 —— 响应不是合法 JSON
            raise ApiError(f"响应不是合法 JSON（{type(exc).__name__}）") from exc
        if not isinstance(data, dict):
            raise ApiError("响应结构异常：顶层不是 JSON 对象")

        content = self._extract_content(data)
        if not content:
            raise ApiError("响应里没有可用的模型输出")
        return content

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _is_response_format_rejected(response: Any) -> bool:
        """判断这个 400 是不是「引擎不认 ``response_format``」。

        v3.2.1 实机踩到：NInfer 默认**不开**结构化输出，
        收到 ``response_format`` 直接回 400：

        .. code-block:: text

            {"error":{"code":"response_format_not_supported",
              "message":"structured output requires the server to start with
                         --structured-output"}}

        认字（``response_format`` 出现在正文里）就够了 ——
        引擎换版本时这句话可能变，但字段名不会变。

        :param response: 响应对象。
        :return: 是这个原因返回 ``True``。
        """
        try:
            text = str(getattr(response, "text", "") or "").lower()
        except Exception:  # noqa: BLE001 —— 取不到正文就不判
            return False
        return "response_format" in text

    def _drop_unsupported_fields(
        self,
        payload: Dict[str, Any],
        response: Any,
    ) -> bool:
        """把引擎**不认**的可选字段从请求体里摘掉（v3.5.0）。

        摘掉之后**记住** —— 以后都不发了，
        省一次注定失败的往返。

        :param payload: 请求体（**原地**修改）。
        :param response: 那个 400 响应。
        :return: 摘掉了什么就返回 ``True``。
        """
        dropped = False
        if self._is_response_format_rejected(response):
            logger.info(
                "引擎不接受 response_format（%s），本次改用普通输出。",
                self.model or "(未命名)",
            )
            self.json_mode = False
            payload.pop("response_format", None)
            dropped = True
        if self.reasoning_effort and self._is_reasoning_effort_rejected(
            response
        ):
            logger.info(
                "引擎不接受 reasoning_effort（%s），本次不关思考。",
                self.model or "(未命名)",
            )
            self.reasoning_effort = ""
            payload.pop("reasoning_effort", None)
            dropped = True
        return dropped

    @staticmethod
    def _is_reasoning_effort_rejected(response: Any) -> bool:
        """判断这个 400 是不是「引擎不认 ``reasoning_effort``」。

        :param response: 响应对象。
        :return: 是这个原因返回 ``True``。
        """
        try:
            text = str(getattr(response, "text", "") or "").lower()
        except Exception:  # noqa: BLE001 —— 取不到正文就不判
            return False
        return "reasoning_effort" in text

    def _extract_content(self, data: Dict[str, Any]) -> str:
        """从响应里取出**最终答案**（**不取**思考过程）。

        v3.2.1 实机踩到（NInfer / Bonsai2-27B）：
        推理模型有两种形态：

        ① 正文在 ``content``、思考在 ``reasoning_content``；
        ② **正文是空的**，答案直接落在 ``reasoning_content`` 里
           —— 这台机器上实测就是如此：1024 个 token 的预算
           全被 ``reasoning_tokens`` 吃光，``content`` 一个字都没有。

        规矩只有一条：**看板的告警文案里不能出现思考过程**。
        所以这里：

        1. 先取 ``content``，把 ``<think>…</think>`` 整段剥掉；
        2. ``content`` 为空时，从 ``reasoning_content`` 里
           **只抠出最后那段 JSON 答案**（抠不出就当没有）；
        3. 思考过程本身**只进 debug 日志**，永远不进告警文案。

        :param data: 解析后的响应体。
        :return: 最终答案文本；没有可用答案时返回空串。
        """
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return str(strip_thinking(str(first.get("text") or "")))

        content = strip_thinking(str(message.get("content") or ""))
        if content:
            return content

        reasoning = str(message.get("reasoning_content") or "")
        if not reasoning:
            return strip_thinking(str(first.get("text") or ""))

        # ⚠️ 正文空 —— 答案可能直接落在 ``reasoning_content`` 里。
        # 只抠**最后那个 JSON 对象**：思考里的散碎 JSON 会被跳过，
        # 抠出来的也一定是模型最后给出的结论。
        candidate = extract_last_json_object(reasoning)
        if candidate:
            logger.debug(
                "正文为空，从 reasoning_content 里取到了 JSON 答案（%d 字符）",
                len(candidate),
            )
            return candidate

        # 思考过程**只留证**，不进告警文案。
        logger.debug(
            "模型只给了思考过程、没有最终答案，已丢弃。思考摘要：%s",
            reasoning[:400],
        )
        return ""

    def _http_error_text(self, status: int, response: Any) -> str:
        """把 HTTP 错误变成一句能指导处置的话。

        ⚠️ **响应正文必须先脱敏**：网关的报错里经常原样回显密钥
        （``invalid api key sk-xxx``），而错误消息会进日志 ——
        不脱敏等于把密钥抄进日志文件。

        双重保险：

        1. 精确遮蔽**本次使用的那个** api_key；
        2. 再跑一遍通用密钥规则（:func:`redact_secrets`）。
        """
        hints = {
            400: "请求参数不被接受（模型名或消息格式不对）",
            401: "鉴权失败（api_key 无效或没配置）",
            403: "没有访问权限（账号欠费或未开通该模型）",
            404: "接口地址或模型名不存在",
            413: "请求过大（上下文超出模型限制）",
            429: "触发限流（稍后重试）",
            500: "服务端内部错误",
            502: "网关错误",
            503: "服务暂时不可用",
        }
        hint = hints.get(status, "请求失败")
        detail = ""
        try:
            text = str(getattr(response, "text", "") or "")
        except Exception:  # noqa: BLE001 —— 取不到正文就算了
            text = ""
        if text:
            # 只截一小段，且先脱敏 —— 错误消息是要进日志的。
            detail = self._mask_key(text[:300])
            detail = " ".join(detail.split())[:120]
        if detail:
            return f"HTTP {status}：{hint}（{detail}）"
        return f"HTTP {status}：{hint}"

    def _mask_key(self, text: str) -> str:
        """遮蔽文本里出现的 api_key 与其它密钥形态。"""
        masked = text
        if self.api_key:
            masked = masked.replace(self.api_key, "«REDACTED»")
        return redact_secrets(masked)

    def _warn_data_leaves_machine(self) -> None:
        """提醒数据流向。

        v3.2.1：**本机引擎不适用**「数据会离开本机」这句 ——
        NInfer 跑在 ``127.0.0.1``，数据一步没出本机。
        这时候还喊「内容将离开本机」是**假话**，
        反而会让人不敢用本地强模型。
        所以本机地址只说一句本地确认。

        :return: 无返回值。
        """
        host = urlparse(self.base_url).hostname or self.base_url
        if self.is_local:
            logger.info(
                "L2 使用本机引擎（%s）：被审查的日志内容不出本机。", host
            )
            return
        logger.warning(
            "L2 使用外部模型 API（%s）：被审查的日志内容将离开本机。"
            "密钥与敏感信息已在送出前脱敏。",
            host,
        )
