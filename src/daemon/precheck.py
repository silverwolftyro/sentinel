"""L1 预检：外部来一段任务文本，秒级判「放行还是拦下」（v5.0.0）。

给谁用
======

外部宿主（比如那个第三方 Agent Bridge 插件）在**真正动手之前**，
把用户交代的那句话发给哨兵问一句「这活儿能做吗」，
哨兵用**纯规则**（L1）秒回：

.. code-block:: json

    {"action": "block", "rule_id": "catastrophic.disk_wipe", "reason": "……"}
    {"action": "allow"}

为什么只上 L1
============

这一层**不调模型** —— 预检要在 Agent 动手**之前**返回，
调模型的话那点延迟就白搭了（而且本地模型可能压根没起）。
所以这里只有纯规则：**红线级命令**（删库、格式化磁盘、往外发密钥…）
与**危险词命中**会被拦下，其余一律放行。

⚠️ **L1 通过 ≠ 安全** —— 它只看字面，不看意图。
真正的语义判定在 L2/L3（那需要模型，见
:class:`~src.daemon.context.SessionContext` 的 ``l2_sleeping``）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.rules.basic import VERDICT_DANGEROUS, RuleEngine

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_ALLOW",
    "ACTION_BLOCK",
    "build_pre_check_event",
    "pre_check",
]

#: 放行。
ACTION_ALLOW = "allow"
#: 拦下。
ACTION_BLOCK = "block"


def build_pre_check_event(text: str) -> Dict[str, Any]:
    """把一段任务文本包装成 L1 能吃的**工具调用**事件。

    ⚠️ 走的是「命令行」这条路（``Bash`` + ``command``）——
    因为高危规则本来就是按命令文本匹配的
    （``rm -rf /``、``mkfs``、``curl | sh`…）。

    :param text: 任务文本。
    :return: 统一事件字典。
    """
    return {
        "type": "tool_call",
        "tool": {
            "name": "Bash",
            "arguments": {"command": str(text or "")},
        },
    }


def pre_check(
    text: str,
    *,
    rules: Optional[RuleEngine] = None,
    config: Optional[Dict[str, Any]] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """对一段任务文本做 L1 秒级判定。

    :param text: 任务文本（用户交代的那句话）。
    :param rules: 复用的规则引擎；不给就现造一个。
    :param config: 已加载的配置字典。
    :param config_path: 配置文件路径（``config`` 为 ``None`` 时生效）。
    :return: 判定字典，必定含 ``action`` 键。
    """
    engine = rules
    if engine is None:
        try:
            engine = RuleEngine(config, config_path=config_path)
        except Exception as exc:  # noqa: BLE001 —— 预检失败必须放行
            logger.warning(
                "预检规则引擎初始化失败（已放行）：%s: %s",
                type(exc).__name__,
                exc,
            )
            return {
                "action": ACTION_ALLOW,
                "verdict": "",
                "rule_id": "",
                "reason": "",
                "evidence": "",
                "degraded": True,
            }

    try:
        verdict = engine.check_event(build_pre_check_event(text))
    except Exception as exc:  # noqa: BLE001 —— 判定失败必须放行
        logger.warning(
            "预检判定失败（已放行）：%s: %s", type(exc).__name__, exc
        )
        return {
            "action": ACTION_ALLOW,
            "verdict": "",
            "rule_id": "",
            "reason": "",
            "evidence": "",
            "degraded": True,
        }

    dangerous = str(verdict.get("verdict") or "") == VERDICT_DANGEROUS
    return {
        "action": ACTION_BLOCK if dangerous else ACTION_ALLOW,
        "verdict": str(verdict.get("verdict") or ""),
        "rule_id": str(verdict.get("rule_id") or ""),
        "reason": str(verdict.get("reason") or ""),
        "evidence": str(verdict.get("evidence") or ""),
        "degraded": False,
    }
