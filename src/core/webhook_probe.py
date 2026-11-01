"""Webhook 真实连通性自检 —— 启动时**真的发一条**出去。

v1.x 一直只跟本地故障注入服务器打交道：
协议对不对、重试退避有没有生效、5xx 会不会再试 ——
这些都验证过了，但**从没打过真实服务**。

所以哨兵启动时，如果用户配了真实的 Webhook，
就**真的发一条**出去，让你确认「手机真的能响」。

为什么值得单独发一条
====================

告警是**偶发**的 —— 等真出事才发现推不出去就太晚了。
启动时发一条，等于**每天（每次开机）自动确认一次**：

* 外网通不通；
* Token / 加签密钥还对不对；
* 钉钉 / 飞书的 **IP 白名单**有没有把你的机器放进来
  （钉钉默认只允许内网 IP，这是最常见的“明明配了却收不到”）。

自检消息里带**版本号**与**本机 IP** ——
版本号告诉你测的是哪一版哨兵；
IP 告诉你该往白名单里填哪几个。

不发的情况
==========

* 通道 ``enabled: false``；
* 没有填 URL；
* 配置 ``alert.webhook_startup_test: false``（嫌吵可以关掉）。

⚠️ 自检消息**会真的出现在你的聊天窗口里** ——
这不是模拟，是**真的**。
"""

from __future__ import annotations

import logging
import socket
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


#: 启动自检的触发标记（写进告警的 ``triggers``，便于事后辨认）。
PROBE_TRIGGER = "webhook_startup_test"


def local_ips() -> List[str]:
    """列出本机所有 IPv4 地址。

    为什么要**全部**列出来：钉钉 / 飞书的 IP 白名单要一个个填，
    只给一个的话，用户往往填了虚拟网卡那个（收不到）。

    :return: IP 列表；探测失败时返回空列表。
    """
    found: List[str] = []

    def _add(address: Any) -> None:
        text = str(address or "").strip()
        if text and text not in found:
            found.append(text)

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            _add(info[4][0])
    except OSError as exc:
        # 反查主机名失败 —— 下面还有「默认出口」那一条路。
        logger.debug(
            "按主机名反查本机 IP 失败：%s: %s", type(exc).__name__, exc
        )
    # 再问一次「默认出口」—— 上面那次常常只拿到虚拟网卡。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.4)
            probe.connect(("8.8.8.8", 80))
            _add(probe.getsockname()[0])
    except OSError as exc:
        # 没有默认出口（断网 / 被防火墙拦）—— 自检会如实说「没 IP」。
        logger.debug(
            "探测默认出口 IP 失败：%s: %s", type(exc).__name__, exc
        )
    return found


def build_probe_alert(version: str = "") -> Dict[str, Any]:
    """造一条自检告警（走和真告警一样的报文路径）。

    :param version: 哨兵版本号。
    :return: 告警字典。
    """
    ips = local_ips()
    ip_text = "、".join(ips) if ips else "（没探测到，请手动 ipconfig）"
    lines = [
        "【哨兵连通性自检】",
        "版本：{}".format(version or "unknown"),
        "本机 IP：{}".format(ip_text),
        "本机名：{}".format(socket.gethostname()),
        "",
        "如果你在手机上看到了这条消息 —— 这条链路是真通的。",
        "把上面这几个 IP 加进机器人的白名单，以后告警就能推到了。",
    ]
    return {
        "verdict": "WEBHOOK_TEST",
        "level": "info",
        "severity": "info",
        "rule_id": "webhook_connectivity_test",
        "reason": "哨兵启动时的 Webhook 连通性自检（不是真实告警）",
        "evidence": "、".join(lines),
        "explanation": "\n".join(lines),
        "command": "",
        "agent": "sentinel",
        "source": "sentinel",
        "triggers": [PROBE_TRIGGER],
        "timestamp": 0.0,
    }


def configured_webhooks(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """挑出配置里**启用了且填了地址**的 webhook 通道。

    :param config: 配置字典。
    :return: 通道条目列表。
    """
    alert = (config or {}).get("alert") or {}
    channels = alert.get("channels") or []
    result: List[Dict[str, Any]] = []
    for entry in channels:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "").lower() != "webhook":
            continue
        if not bool(entry.get("enabled", False)):
            continue
        if not str(entry.get("url") or "").strip():
            continue
        result.append(entry)
    return result


def probe_webhooks(
    config: Dict[str, Any], version: str = ""
) -> List[Dict[str, Any]]:
    """给每个真实 Webhook **真的发一条**自检消息。

    **绝不抛异常** —— 自检失败不该挡住哨兵启动（架构红线 R5）。

    :param config: 配置字典。
    :param version: 哨兵版本号。
    :return: 每个通道一条结果
        （``{"url", "ok", "detail", "status", "body"}``）。
    """
    results: List[Dict[str, Any]] = []
    entries = configured_webhooks(config)
    if not entries:
        return results

    try:
        from .channels import WebhookChannel
    except Exception as exc:  # noqa: BLE001 —— 导入失败只降级
        logger.warning("webhook 自检跳过（导入失败）：%s", exc)
        return results

    alert = build_probe_alert(version)
    for entry in entries:
        url = str(entry.get("url") or "")
        item: Dict[str, Any] = {
            "url": url,
            "format": str(entry.get("format") or "generic"),
            "ok": False,
            "detail": "",
            "status": 0,
            "body": "",
        }
        try:
            channel = WebhookChannel(
                url=url,
                timeout=float(entry.get("timeout") or 10),
                format=item["format"],
                card=bool(entry.get("card", False)),
                secret=str(entry.get("secret") or ""),
                # 自检**不受**触发类型过滤 ——
                # 用户配了就是想确认能收到。
                triggers=None,
                dedup_seconds=0,
                max_retries=0,
            )
            accepted = channel.send(alert)
            if not accepted:
                item["detail"] = "投递请求没被接受（地址不合法？）。"
            else:
                channel.flush(timeout=float(entry.get("timeout") or 10) + 8.0)
                response = dict(getattr(channel, "last_response", {}) or {})
                item["status"] = int(response.get("status") or 0)
                item["body"] = str(response.get("body") or "")[:400]
                item["ok"] = bool(getattr(channel, "last_delivered", False))
                item["detail"] = (
                    "已送达" if item["ok"]
                    else "投递失败：{}".format(
                        str(getattr(channel, "last_error", "") or "未知原因")
                    )
                )
            channel.close()
        except Exception as exc:  # noqa: BLE001 —— 自检失败只降级
            item["detail"] = "自检异常：{}: {}".format(type(exc).__name__, exc)
        results.append(item)
    return results


def startup_probe(
    config: Dict[str, Any], version: str = ""
) -> Optional[List[Dict[str, Any]]]:
    """启动时的连通性自检（带开关）。

    :param config: 配置字典。
    :param version: 哨兵版本号。
    :return: 结果列表；**没配置 / 关掉了开关**时返回 ``None``。
    """
    alert = (config or {}).get("alert") or {}
    if not bool(alert.get("webhook_startup_test", True)):
        return None
    entries = configured_webhooks(config)
    if not entries:
        return None
    return probe_webhooks(config, version)
