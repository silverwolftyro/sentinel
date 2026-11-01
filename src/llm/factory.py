"""L2 模型提供方工厂 —— 按配置挑底层实现。

哨兵支持两种 L2 后端：

===================== ==================================================
``llm.provider``      底层
===================== ==================================================
``ollama``            本地 Ollama（默认）—— 数据不出本机
``openai_compatible`` 任意 OpenAI 兼容 API —— DeepSeek / Kimi / 硅基流动 /
                      自建 vLLM / one-api 网关等
===================== ==================================================

上层（引擎、看板）**只认** :class:`~src.llm.base.LLMProvider` 这一个接口，
因此换后端不需要改任何调用方 —— 这正是 v0.6.0 抽象它的目的。

v1.2.0 起还支持**多级路由**（:mod:`src.llm.router`）：
配置里写一个 ``llm.routing.fallback``，平时用 ``llm.provider`` 指定的
主力模型省钱，一旦红线级拦截或连续漂移就自动升级到强模型。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..config_loader import load_config
from .base import LLMProvider

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDERS",
    "build_cascade_provider",
    "build_provider",
    "build_routing_provider",
    "normalize_provider",
]

#: 受支持的提供方标识 → 规范化名称。
PROVIDERS: Dict[str, str] = {
    "ollama": "ollama",
    "local": "ollama",
    "openai_compatible": "openai_compatible",
    "openai-compatible": "openai_compatible",
    "openai": "openai_compatible",
}


def normalize_provider(raw: Any) -> str:
    """把配置里的提供方名规范化。

    :param raw: 配置值。
    :return: ``ollama`` 或 ``openai_compatible``；无法识别时回落到
        ``ollama``（本地是更安全的一侧）。
    """
    key = str(raw or "").strip().lower()
    return PROVIDERS.get(key, "ollama")


def build_provider(
    config: Optional[Dict[str, Any]] = None,
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    enabled: Optional[bool] = None,
    session: Optional[Any] = None,
) -> LLMProvider:
    """按配置构造 L2 提供方。

    :param config:   已加载的配置字典；为 ``None`` 时自行读取。
    :param model:    覆盖配置里的模型名。
    :param timeout:  覆盖配置里的超时。
    :param enabled:  覆盖配置里的启用开关。
    :param session:  注入的 ``requests.Session``（测试用）。
    :return: 提供方实例（:class:`~src.llm.base.LLMProvider`）。
    :raises ValueError: 配置里的提供方名无法识别。

    .. note::
       无法识别的提供方名**不会**静默回落 —— 那会让用户以为自己在
       用外部 API、其实却在本地跑，或者反过来。所以这里直接报错，
       由调用方降级处理。
    """
    if config is None:
        config = load_config()
    llm_config = (config.get("llm") or {}) if isinstance(config, dict) else {}
    raw_provider = llm_config.get("provider")
    key = str(raw_provider or "ollama").strip().lower()
    normalized = normalize_provider(raw_provider)
    if key and normalized == "ollama" and key not in PROVIDERS:
        raise ValueError(
            f"未知的 llm.provider：{raw_provider!r}"
            f"（可选：{', '.join(sorted(set(PROVIDERS.values())))}）"
        )

    if normalized == "openai_compatible":
        from .api_client import OpenAICompatibleClient

        return OpenAICompatibleClient(
            model=model,
            timeout=timeout,
            enabled=enabled,
            session=session,
            config=config,
        )

    from .ollama_client import OllamaClient

    return OllamaClient(
        model=model,
        timeout=timeout,
        enabled=enabled,
        session=session,
        config=config,
    )


def build_routing_provider(
    config: Optional[Dict[str, Any]] = None,
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    enabled: Optional[bool] = None,
    session: Optional[Any] = None,
) -> LLMProvider:
    """按配置构造 L2 提供方，**必要时套一层多级路由**。

    路由的装配规则：

    1. ``llm.routing.enabled`` 为真（默认）**且**
       ``llm.routing.fallback`` 配了东西 → 套 :class:`ModelRouter`；
    2. 否则 → 就是一个普通的 provider（行为与 v1.1.0 完全一致）。

    :param config:   已加载的配置字典；为 ``None`` 时自行读取。
    :param model:    覆盖主力模型名。
    :param timeout:  覆盖主力超时。
    :param enabled:  覆盖启用开关。
    :param session:  注入的 ``requests.Session``（测试用）。
    :return: 提供方实例。
    """
    if config is None:
        config = load_config()
    llm_config = (config.get("llm") or {}) if isinstance(config, dict) else {}
    routing = llm_config.get("routing")
    routing = routing if isinstance(routing, dict) else {}
    fallback_block = routing.get("fallback")
    fallback_block = fallback_block if isinstance(fallback_block, dict) else {}

    primary = build_provider(
        config, model=model, timeout=timeout, enabled=enabled, session=session
    )

    if not bool(routing.get("enabled", True)) or not fallback_block:
        return primary

    try:
        fallback = _build_fallback(fallback_block, session=session)
    except Exception as exc:  # noqa: BLE001 —— 升级模型配错不该拖垮主流程
        logger.warning(
            "升级模型（llm.routing.fallback）装配失败，本次只用主力模型：%s: %s",
            type(exc).__name__,
            exc,
        )
        return primary
    if fallback is None:
        return primary

    from .router import ModelRouter

    router = ModelRouter(
        primary,
        fallback,
        escalate_on_red_line=bool(routing.get("escalate_on_red_line", True)),
        drift_escalate_threshold=int(
            routing.get("drift_escalate_threshold", 1)
        ),
    )
    logger.info(
        "L2 多级路由已启用：主力 %s，升级 %s",
        getattr(primary, "model", "") or "(未命名)",
        getattr(fallback, "model", "") or "(未命名)",
    )
    return router


def _build_fallback(
    block: Dict[str, Any],
    *,
    session: Optional[Any] = None,
) -> Optional[LLMProvider]:
    """按 ``llm.routing.fallback`` 这块配置造一个升级用的提供方。

    刻意**不复用**顶层 ``llm`` 段的字段 —— 主力是本地、升级是云端，
    两者的 ``base_url`` / ``model`` / ``api_key`` 完全不是一回事。

    :param block:  ``llm.routing.fallback`` 配置块。
    :param session: 注入的 ``requests.Session``。
    :return: 提供方实例；配置不可用时返回 ``None``。
    """
    provider = str(block.get("provider") or "openai_compatible").strip().lower()
    if provider in ("ollama", "local"):
        # 本地模型也能当「升级」用（比如主力是 7B、升级是 32B）。
        from .ollama_client import OllamaClient

        sub_config = {"llm": dict(block)}
        return OllamaClient(config=sub_config, session=session)

    if provider not in ("openai_compatible", "openai-compatible", "openai"):
        # ⚠️ 名字不认识要**报错**，不能静默当成云端 ——
        # 那会让用户以为自己在用某个特定厂商的模型，其实却在调别家。
        # 上层（build_routing_provider）会接住这个异常并退回主力模型。
        raise ValueError(
            f"未知的 llm.routing.fallback.provider：{block.get('provider')!r}"
            "（可选：ollama / openai_compatible）"
        )

    from .api_client import OpenAICompatibleClient

    sub_config = {"llm": dict(block)}
    # 允许配置里单独指定密钥环境变量名。
    env_name = str(block.get("api_key_env") or "").strip()
    api_key = ""
    if env_name:
        import os

        api_key = os.environ.get(env_name, "")
    return OpenAICompatibleClient(
        api_key=api_key or None,
        session=session,
        config=sub_config,
    )


def _build_tier(
    block: Dict[str, Any],
    *,
    session: Optional[Any] = None,
) -> Optional[LLMProvider]:
    """按 ``llm.cascade.l2`` / ``llm.cascade.l3`` 这块配置造一个提供方。

    刻意**不复用**顶层 ``llm`` 段的字段 ——
    L2 是本地极速过滤、L3 是云端终审，
    两者的 ``base_url`` / ``model`` / ``api_key`` / ``timeout``
    完全不是一回事。

    ⚠️ **超时下限 = 超时本身** ——
    客户端对本机引擎有一个 600 秒的超时下限
    （27B 推理模型思考起来很慢，要给它时间）。
    但 L2 是「10 秒就放弃」的极速过滤，
    那个下限会让它**永远等不满** —— 所以这里把下限显式设成自己的超时。

    :param block:  配置块（``llm.cascade.l2`` 或 ``llm.cascade.l3``）。
    :param session: 注入的 ``requests.Session``。
    :return: 提供方实例；配置不可用时返回 ``None``。
    """
    provider = str(block.get("provider") or "openai_compatible").strip().lower()
    timeout = block.get("timeout")
    floor = block.get("timeout_floor", timeout)

    if provider in ("ollama", "local"):
        from .ollama_client import OllamaClient

        sub_config = {"llm": dict(block)}
        return OllamaClient(config=sub_config, session=session)

    if provider not in ("openai_compatible", "openai-compatible", "openai"):
        raise ValueError(
            f"未知的级联模型 provider：{block.get('provider')!r}"
            "（可选：ollama / openai_compatible）"
        )

    from .api_client import OpenAICompatibleClient

    sub_config = {"llm": dict(block)}
    env_name = str(block.get("api_key_env") or "").strip()
    api_key = ""
    if env_name:
        import os

        api_key = os.environ.get(env_name, "")
    return OpenAICompatibleClient(
        api_key=api_key or None,
        session=session,
        config=sub_config,
        timeout_floor=float(floor) if floor is not None else None,
    )


def build_cascade_provider(
    config: Optional[Dict[str, Any]] = None,
    *,
    session: Optional[Any] = None,
    goal: str = "",
) -> Optional[Any]:
    """按配置构造 **L2 → L3 级联**编排器（v3.5.0）。

    配置形如：

    .. code-block:: yaml

        llm:
          cascade:
            enabled: true
            l2:            # 本地极速过滤
              provider: "openai_compatible"
              base_url: "http://127.0.0.1:8888/v1"
              model: "<本地小模型>"
              timeout: 10
            l3:            # 云端终审
              provider: "openai_compatible"
              base_url: "https://api.example.com/v1"
              model: "<云端模型>"
              api_key_env: "SENTINEL_LLM_API_KEY"
              timeout: 30

    ⚠️ **没开 ``llm.cascade.enabled`` 就返回 ``None``** ——
    调用方据此「级联没开」，走**原来那条路**，
    行为与 v3.4.0 **完全一样**。

    :param config:  已加载的配置字典；为 ``None`` 时自行读取。
    :param session: 注入的 ``requests.Session``（测试用）。
    :param goal:    任务目标（进静态前缀）。
    :return: :class:`~src.llm.cascade.CascadeRouter`；没开就返回 ``None``。
    """
    if config is None:
        config = load_config()
    llm_config = (config.get("llm") or {}) if isinstance(config, dict) else {}
    cascade_cfg = llm_config.get("cascade") or {}
    if not isinstance(cascade_cfg, dict):
        return None
    if not bool(cascade_cfg.get("enabled", False)):
        return None

    l2_block = cascade_cfg.get("l2")
    l2_block = l2_block if isinstance(l2_block, dict) else {}
    l3_block = cascade_cfg.get("l3")
    l3_block = l3_block if isinstance(l3_block, dict) else {}

    # --- v3.7.0：L2 / L3 各自独立开关 ---
    #
    # 「关掉 L2」是用户明确表达过的场景（本地显存腾不出来），
    # 所以这里**不装配** L2 —— 留着它只会白占显存。
    l2_enabled = bool(l2_block.get("enabled", True))
    l3_enabled = bool(l3_block.get("enabled", True))
    bridge = bool(cascade_cfg.get("bridge_l1_to_l3", False))

    if l2_enabled and not l2_block:
        logger.warning(
            "llm.cascade.enabled=true 但没配 llm.cascade.l2 —— 级联不启用。"
        )
        return None
    if not l2_enabled and l3_enabled and not l3_block:
        logger.warning(
            "llm.cascade.l2 已关闭，但也没配 llm.cascade.l3 —— 级联不启用。"
        )
        return None
    if not l2_enabled and not l3_enabled:
        logger.warning("llm.cascade 的 L2 与 L3 都关着 —— 级联不启用。")
        return None
    if bridge and (l2_enabled or not l3_enabled):
        logger.warning(
            "bridge_l1_to_l3 需要「L2 关闭 + L3 开启」才生效，本次忽略该桥接。"
        )
        bridge = False

    from .cascade import CascadeRouter

    l2 = None
    if l2_enabled:
        try:
            l2 = _build_tier(l2_block, session=session)
        except Exception as exc:  # noqa: BLE001 —— 配错不该拖垮审查
            logger.warning(
                "L2 模型装配失败，级联不启用：%s: %s", type(exc).__name__, exc
            )
            return None
        if l2 is None:
            return None

    l3 = None
    if l3_enabled:
        try:
            l3 = _build_tier(l3_block, session=session)
        except Exception as exc:  # noqa: BLE001 —— 配错不该拖垮审查
            logger.warning(
                "L3 模型装配失败，本次只用 L2：%s: %s",
                type(exc).__name__,
                exc,
            )

    logger.info(
        "级联审核已启用：L2=%s，L3=%s，桥接 L1→L3=%s",
        getattr(l2, "model", "") or ("(已关闭)" if not l2_enabled else "(未命名)"),
        getattr(l3, "model", "") or ("(已关闭)" if not l3_enabled else "(未配置)"),
        bridge,
    )
    return CascadeRouter(
        l2,
        l3,
        goal=goal,
        require_agreement=bool(cascade_cfg.get("require_agreement", True)),
        breaker_threshold=int(cascade_cfg.get("breaker_threshold", 3)),
        breaker_open_seconds=float(cascade_cfg.get("breaker_open_seconds", 60)),
        use_l2=l2_enabled,
        use_l3=l3_enabled,
        bridge_l1_to_l3=bridge,
        # --- v3.8.0：L2 掉线热切 L3 + 回退预算熔断 ---
        failover_to_l3=bool(cascade_cfg.get("failover_to_l3", True)),
        l3_fallback_limit=int(cascade_cfg.get("l3_fallback_limit", 100)),
        l3_fallback_limit_seconds=float(
            cascade_cfg.get("l3_fallback_limit_seconds", 3600)
        ),
        l3_fallback_cost_limit=float(
            cascade_cfg.get("l3_fallback_cost_limit", 0)
        ),
        l3_fallback_unit_cost=float(cascade_cfg.get("l3_fallback_unit_cost", 0)),
    )
