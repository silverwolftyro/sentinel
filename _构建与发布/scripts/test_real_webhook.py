"""真实 Webhook 终极触达测试 —— 打钉钉 / 飞书 / Bark / Server酱 的真服务。

v1.x 一直只跟**本地故障注入服务器**打交道：
协议对不对、重试退避有没有生效、5xx 会不会再试 ——
这些都验证过了，但**从没打过真实服务**。

所以这个脚本存在的意义就一件事：
**把一条真的消息发出去**，让你的手机上真的响一声。

怎么用
======

最简单 —— 直接贴地址：

.. code-block:: bash

   python _构建与发布/scripts/test_real_webhook.py \\
       --url "https://oapi.dingtalk.com/robot/send?access_token=你的token"

已经有配置文件了 —— 让它读：

.. code-block:: bash

   python _构建与发布/scripts/test_real_webhook.py --list
   python _构建与发布/scripts/test_real_webhook.py --use-config

只看看配置里有哪些、**不发送**：

.. code-block:: bash

   python _构建与发布/scripts/test_real_webhook.py --list

钉钉 / 飞书开了「加签」就补上密钥：

.. code-block:: bash

   python _构建与发布/scripts/test_real_webhook.py \\
       --url "https://oapi.dingtalk.com/robot/send?access_token=xxx" \\
       --secret "SECxxxx"

它会打印什么
============

* **本地 IP**（多网卡时全列出来）——
  钉钉 / 飞书机器人默认只允许**内网 IP** 推消息，
  公网 IP 会被拒（错误码 ``310000``），所以这条信息很有用；
* **版本号** —— 确认你测的是哪一版哨兵；
* **HTTP 状态码与响应体** —— 钉钉 / 飞书会回 ``errcode``，
  ``0`` 才算成功；非 0 时脚本会**逐条解释**常见错误码。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any, Dict, List, Optional, Tuple

# 让脚本能从项目根 import 到 src 包。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.config_loader import load_config  # noqa: E402

#: 各家 Webhook 的常见错误码 → 人话。
ERROR_HINTS: Dict[Tuple[str, int], str] = {
    ("dingtalk", 300001): "关键词不匹配 —— 机器人里要加上告警里的关键词。",
    ("dingtalk", 310000): "IP 不在白名单 —— 钉钉只允许内网 IP 推消息。",
    ("dingtalk", 310000 + 1): "关键词不匹配。",
    ("dingtalk", 400013): "加签不对 —— 检查 secret 是不是设成了机器人那一串。",
    ("feishu", 19021): "签名不匹配 —— 检查 secret 与时间戳。",
    ("feishu", 9499): "机器人被封禁 / 未发布。",
    ("feishu", 19020): "关键词不匹配 —— 机器人配置里要放关键词。",
}


def local_ips() -> List[str]:
    """列出本机所有 IPv4 地址（尽量全，方便填白名单）。

    :return: IP 列表；一个都没有时返回空列表。
    """
    found: List[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = str(info[4][0])
            if address not in found:
                found.append(address)
    except OSError:
        pass
    # 再问一次「默认出口」—— 上面那次常常只拿到虚拟网卡。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.4)
            probe.connect(("8.8.8.8", 80))
            address = str(probe.getsockname()[0])
            if address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def version_text() -> str:
    """取哨兵的版本号。

    :return: 形如 ``v2.1.0`` 的版本串；取不到时返回 ``unknown``。
    """
    try:
        from src import __version__  # type: ignore

        return "v" + str(__version__)
    except Exception:  # noqa: BLE001 —— 取不到版本不该挡住测试
        return "unknown"


def configured_webhooks(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从配置里挑出**真的填了地址**的 webhook 通道。

    :param config: 配置字典。
    :return: 通道条目列表（每条都带 ``url``）。
    """
    alert = config.get("alert") or {}
    channels = alert.get("channels") or []
    result: List[Dict[str, Any]] = []
    for entry in channels:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "").lower() != "webhook":
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        result.append(entry)
    return result


def build_message(webhook_format: str, card: bool) -> Dict[str, Any]:
    """造一条测试告警（走和真告警一样的报文路径）。

    :param webhook_format: 报文格式（``dingtalk`` / ``feishu`` …）。
    :param card: 是否发卡片。
    :return: 告警字典。
    """
    ips = local_ips()
    ip_text = "、".join(ips) if ips else "（没探测到，请手动 ipconfig）"
    lines = [
        "【哨兵连通性自检】",
        "版本：{}".format(version_text()),
        "本机 IP：{}".format(ip_text),
        "本机名：{}".format(socket.gethostname()),
        "报文格式：{}".format(webhook_format),
        "卡片：{}".format("是" if card else "否"),
        "",
        "如果你在手机上看到了这条消息 —— ",
        "说明这条链路是真通的（外网连通性 OK）。",
    ]
    return {
        "verdict": "WEBHOOK_TEST",
        "level": "info",
        "severity": "info",
        "rule_id": "webhook_connectivity_test",
        "reason": "哨兵 Webhook 连通性自检（不是真实告警）",
        "evidence": "、".join(lines),
        "explanation": "\n".join(lines),
        "command": "",
        "agent": "sentinel",
        "source": "sentinel",
        "triggers": ["webhook_test"],
        "timestamp": 0.0,
    }


def send_once(
    url: str,
    webhook_format: str,
    *,
    secret: str = "",
    card: bool = False,
    timeout: float = 10.0,
) -> Tuple[bool, str]:
    """真的发一条出去。

    :param url: 接收地址。
    :param webhook_format: 报文格式。
    :param secret: 加签密钥。
    :param card: 是否发卡片。
    :param timeout: 超时（秒）。
    :return: ``(是否成功, 说明文字)``。
    """
    from src.core.channels import WebhookChannel

    channel = WebhookChannel(
        url=url,
        timeout=timeout,
        format=webhook_format,
        card=card,
        secret=secret,
        # 测试消息**不受**触发类型过滤 —— 用户就是想试一下。
        triggers=None,
        dedup_seconds=0,
        max_retries=0,
    )
    alert = build_message(webhook_format, card)
    try:
        accepted = channel.send(alert)
    except Exception as exc:  # noqa: BLE001 —— 测试脚本要给出人话
        return False, "投递时抛异常：{}: {}".format(type(exc).__name__, exc)
    if not accepted:
        return False, "投递请求没被接受（地址为空 / 协议不支持 / 队列满了）。"
    # 后台线程在投递 —— 等它把队列跑完，再读结果。
    try:
        channel.flush(timeout=timeout + 8.0)
    finally:
        channel.close()
    response = dict(getattr(channel, "last_response", {}) or {})
    status = int(response.get("status") or 0)
    body = str(response.get("body") or "")
    delivered = bool(getattr(channel, "last_delivered", False))
    error = str(getattr(channel, "last_error", "") or "")
    if not delivered:
        return False, "投递失败：{}（HTTP {}）".format(error or "未知原因", status)
    detail = "HTTP {} · {}".format(status, body[:400] or "(空响应体)")
    if body:
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            code = payload.get("errcode", payload.get("code"))
            if code is not None and str(code) not in ("0", "None"):
                return False, "服务端拒收了：{} · 错误码 {}：{}".format(
                    detail,
                    code,
                    ERROR_HINTS.get(
                        (webhook_format, int(code)), "见各家官方文档。"
                    ),
                )
    return True, detail


def _ask(prompt: str) -> str:
    """问用户一句话（非交互环境返回空串）。

    :param prompt: 提示语。
    :return: 用户输入。
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    """脚本入口。

    :param argv: 命令行参数（默认取 ``sys.argv``）。
    :return: 退出码（0 = 成功）。
    """
    parser = argparse.ArgumentParser(
        description="哨兵 · 真实 Webhook 触达测试（钉钉 / 飞书 / Bark / Server酱）",
    )
    parser.add_argument("--config", default="", help="哨兵配置文件路径")
    parser.add_argument("--url", default="", help="Webhook 地址（直接贴 token 也行）")
    parser.add_argument(
        "--format",
        default="",
        help="报文格式：dingtalk / feishu / serverchan / bark / generic",
    )
    parser.add_argument("--secret", default="", help="钉钉 / 飞书的加签密钥")
    parser.add_argument("--card", action="store_true", help="发 Markdown 卡片")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="超时（秒），默认 10"
    )
    parser.add_argument(
        "--list", action="store_true", help="只列出配置里的 webhook，不发送"
    )
    parser.add_argument(
        "--use-config",
        action="store_true",
        help="用配置文件里的第一条 webhook 发送",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config or None)
    entries = configured_webhooks(config)

    print("=" * 62)
    print("  哨兵 · 真实 Webhook 触达测试")
    print("=" * 62)
    print("版本：{}".format(version_text()))
    ips = local_ips()
    print("本机 IP：{}".format("、".join(ips) if ips else "（没探测到）"))
    print("本机名：{}".format(socket.gethostname()))
    print("")

    if args.list or (not args.url and not args.use_config):
        if entries:
            print("配置文件里找到这些 webhook：")
            for index, entry in enumerate(entries, start=1):
                print(
                    "  [{}] {} · 格式={} · 卡片={}".format(
                        index,
                        entry.get("url"),
                        entry.get("format") or "generic",
                        bool(entry.get("card")),
                    )
                )
        else:
            print("配置文件里没有填了地址的 webhook 通道。")
        if args.list:
            print("")
            print("（--list 只看不发。要真发一条，加 --use-config 或 --url。）")
            return 0

    url = str(args.url or "")
    webhook_format = str(args.format or "")
    secret = str(args.secret or "")
    card = bool(args.card)

    if not url:
        if not entries:
            print("没有可用的 webhook —— 请用 --url 贴一个地址。")
            return 2
        if args.use_config:
            entry = entries[0]
            url = str(entry.get("url") or "")
            webhook_format = webhook_format or str(entry.get("format") or "")
            secret = secret or str(entry.get("secret") or "")
            card = card or bool(entry.get("card"))
        else:
            print("从下面选一个（直接回车取消）：")
            for index, entry in enumerate(entries, start=1):
                print("  [{}] {}".format(index, entry.get("url")))
            choice = _ask("输入序号：")
            if not choice.isdigit():
                print("已取消。")
                return 0
            picked = entries[int(choice) - 1]
            url = str(picked.get("url") or "")
            webhook_format = webhook_format or str(picked.get("format") or "")
            secret = secret or str(picked.get("secret") or "")
            card = card or bool(picked.get("card"))

    if not webhook_format:
        webhook_format = "generic"
        for entry in entries:
            if str(entry.get("url") or "") == url:
                webhook_format = str(entry.get("format") or "generic")
                break
        if "dingtalk" in url:
            webhook_format = "dingtalk"
        elif "feishu" in url or "lark" in url:
            webhook_format = "feishu"
        elif "ftqq" in url:
            webhook_format = "serverchan"
        elif "day.app" in url:
            webhook_format = "bark"

    print("目标　：{}".format(url))
    print("格式　：{}".format(webhook_format))
    print("加签　：{}".format("有" if secret else "无"))
    print("")
    print("正在发送 …")
    ok, detail = send_once(
        url,
        webhook_format,
        secret=secret,
        card=card,
        timeout=args.timeout,
    )
    print("")
    print("-" * 62)
    print("结果　：{}".format("成功" if ok else "失败"))
    print("详情　：{}".format(detail))
    print("-" * 62)
    if ok:
        print("外网连通性 OK —— 你应该已经在手机上看到那条消息了。")
    else:
        print("没发出去。先看上面的错误码与提示；")
        print("钉钉 / 飞书最常见的原因是**内网 IP 白名单**没配。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
