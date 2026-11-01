#!/usr/bin/env python
"""NInfer 连接测试 —— 证明哨兵能真的调到本地 NInfer 并给出人话解释。

为什么要有这个脚本
==================

v3.2.1：哨兵要适配本机新装的 **NInfer（Bonsai2-27B）** 审核引擎。
这个引擎的脾气和 Ollama 完全不同：

* 上下文 128K，但**单并发**（``max-concurrency=1``）——
  同时发两个请求，引擎**直接崩**；
* 首次**预填充极慢**，超时必须拉满（600 秒）；
* 输出里可能带 ``<think>`` 思考段或 ``reasoning_content`` 字段 ——
  思考过程**不能**进看板的告警文案。

光靠「读代码猜」不行 —— 必须**实际打一次**，把真实响应结构看清楚：

* 响应里到底有没有 ``content``？还是只有 ``reasoning_content``？
* ``<think>`` 是内嵌在 ``content`` 里，还是独立字段？
* ``response_format={"type":"json_object"}`` 它认不认？
* 预填充到底要多久？

本脚本做三件事：

1. ``GET /v1/models`` —— 看引擎在不在、模型叫什么、上下文多大；
2. **裸 POST** 一次 ``/v1/chat/completions`` —— 把**原始响应**原样打印；
3. **走哨兵自己的 L2 通路**（:func:`build_provider`）再跑一次 ——
   看解析、脱敏、降级之后，最终落到看板上的那段人话是什么样子。

用法
====

.. code-block:: bash

    python _构建与发布/scripts/ninfer_probe.py
    python _构建与发布/scripts/ninfer_probe.py --json "$env:TEMP\\ninfer-probe.json"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.llm.factory import build_provider  # noqa: E402
from src.llm.prompts import (  # noqa: E402
    SENTINEL_EXPLAIN_TEMPLATE,
    build_explain_prompt,
)

#: 默认连接参数（NInfer 本机默认端口）。
DEFAULT_BASE_URL = "http://127.0.0.1:8888/v1"
DEFAULT_MODEL = "bonsai2-27b"
DEFAULT_API_KEY = "sk-no-key-required"

#: 预填充很慢 —— 超时必须拉满（v3.2.1 实机要求）。
DEFAULT_TIMEOUT = 600.0


def _prepare_stdout() -> None:
    """控制台可能是 GBK —— 先切 UTF-8，否则中文全是乱码。

    v3.2.1 实机踩过：脚本 `print` 出来的是 GBK 字节，
    上层按 UTF-8 解码 → 满屏「���Ӳ���」，根本读不懂。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (ValueError, OSError, AttributeError):
            pass


def _out(text: str = "") -> None:
    """打印一行（Windows 控制台默认 GBK，中文可能编不出来）。

    :param text: 要打印的文本。
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))
        sys.stdout.flush()


def _chat_endpoint(base_url: str) -> str:
    """算出 ``/chat/completions`` 的完整地址。

    :param base_url: API 根地址。
    :return: 完整端点。
    """
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def probe_models(base_url: str, api_key: str, timeout: float) -> Dict[str, Any]:
    """问引擎「你是谁」—— 模型列表。

    :param base_url: API 根地址。
    :param api_key:  API 密钥。
    :param timeout: 超时秒数。
    :return: ``{"ok": bool, "models": [...], "error": str}``。
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": "Bearer {}".format(api_key)}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 探测失败不算致命
        return {"ok": False, "models": [], "error": "{}: {}".format(
            type(exc).__name__, exc
        )}
    if int(response.status_code) >= 400:
        return {"ok": False, "models": [], "error": "HTTP {}".format(
            response.status_code
        )}
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "models": [], "error": "响应不是 JSON：{}".format(exc)}
    models = []
    for item in (data.get("data") or []):
        if not isinstance(item, dict):
            continue
        models.append(
            {
                "id": item.get("id"),
                "context_window": item.get("context_window")
                or item.get("max_model_len"),
                "status": item.get("status"),
            }
        )
    return {"ok": True, "models": models, "error": ""}


def probe_chat(
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    json_mode: bool,
    max_tokens: int = 1024,
    nudge: str = "",
) -> Dict[str, Any]:
    """裸打一次 ``/chat/completions``，把**原始响应**抓回来。

    :param base_url:  API 根地址。
    :param api_key:  API 密钥。
    :param model:    模型名。
    :param timeout:  超时秒数。
    :param json_mode: 是否发送 ``response_format``。
    :return: 探测结果字典。
    """
    user_prompt = build_explain_prompt(
        thinking="准备把解析器拆成词法与语法两层，先写词法层的单元测试。",
        tool_name="Bash",
        command="rm -rf ./build/cache",
        rule_facts={
            "verdict": "DANGEROUS",
            "rule_id": "dangerous_command",
            "level": "critical",
            "reason": "匹配到高危命令：递归强制删除",
            "evidence": "rm -rf ./build/cache",
        },
    )
    payload: Dict[str, Any] = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SENTINEL_EXPLAIN_TEMPLATE},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if nudge:
        # ⚠️ NInfer 的**提示缓存不认 max_tokens** ——
        # 同样的提示会直接命中旧结果（连截断都照抄）。
        # 加个扰动才能测到「真的重新生成」是什么样。
        payload["messages"][1]["content"] += "\n\n[probe-nudge] {}".format(nudge)

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer {}".format(api_key),
    }
    started = time.monotonic()
    try:
        response = requests.post(
            _chat_endpoint(base_url),
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "elapsed": round(time.monotonic() - started, 2),
            "raw": None,
            "status": 0,
        }
    # ⚠️ 引擎不认 ``response_format``（NInfer 默认就这样）→
    # **退一步**摘掉字段重试 —— 这正是哨兵客户端的同一套动作。
    if (
        int(getattr(response, "status_code", 0) or 0) == 400
        and json_mode
        and "response_format" in str(getattr(response, "text", "") or "").lower()
    ):
        payload.pop("response_format", None)
        try:
            response = requests.post(
                _chat_endpoint(base_url),
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "elapsed": round(time.monotonic() - started, 2),
                "raw": None,
                "status": 0,
            }
    elapsed = round(time.monotonic() - started, 2)
    try:
        raw = response.json()
    except Exception:  # noqa: BLE001
        raw = {"_raw_text": response.text[:2000]}
    body = ""
    if int(response.status_code) >= 400:
        try:
            body = " ".join(str(response.text).split())[:400]
        except Exception:  # noqa: BLE001 —— 取不到正文就算了
            body = ""
    return {
        "ok": int(response.status_code) < 400,
        "error": (
            "HTTP {}{}".format(
                response.status_code,
                "（{}）".format(body) if body else "",
            )
        )
        if int(response.status_code) >= 400
        else "",
        "elapsed": elapsed,
        "status": int(response.status_code),
        "raw": raw,
    }


def summarize_raw(raw: Any) -> List[str]:
    """把裸响应里「模型到底把话放在哪」讲清楚。

    :param raw: 原始响应（字典）。
    :return: 说明行列表。
    """
    lines: List[str] = []
    if not isinstance(raw, dict):
        return ["（响应不是 JSON 对象）"]
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ["（响应里没有 choices）"]
    first = choices[0]
    if not isinstance(first, dict):
        return ["（choices[0] 不是对象）"]
    message = first.get("message")
    if not isinstance(message, dict):
        return ["（choices[0].message 不是对象）"]
    content = str(message.get("content") or "")
    reasoning = str(message.get("reasoning_content") or "")
    lines.append("content 长度          ：{}".format(len(content)))
    lines.append("reasoning_content 长度：{}".format(len(reasoning)))
    lines.append("content 里含 <think>   ：{}".format("是" if "<think>" in content else "否"))
    lines.append("content 前 240 字     ：{}".format(content[:240].replace("\n", " ")))
    if reasoning:
        lines.append("reasoning 前 240 字   ：{}".format(reasoning[:240].replace("\n", " ")))
    return lines


def run_provider(
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    max_tokens: int = 1024,
    json_mode: bool = True,
) -> Dict[str, Any]:
    """走哨兵自己的 L2 通路跑一次，看**最终落到看板上的那段话**。

    :param base_url: API 根地址。
    :param api_key: API 密钥。
    :param model:   模型名。
    :param timeout: 超时秒数。
    :return: 结果字典。
    """
    config = {
        "llm": {
            "provider": "openai_compatible",
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "timeout": timeout,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
            "enabled": True,
            "redact_before_llm": True,
        }
    }
    provider = build_provider(config)
    event = {
        "thinking": "准备把解析器拆成词法与语法两层，先写词法层的单元测试。",
        "tool": {
            "name": "Bash",
            "command": "rm -rf ./build/cache",
        },
        "verdict": {
            "verdict": "DANGEROUS",
            "rule_id": "dangerous_command",
            "level": "critical",
            "severity": "standard",
            "reason": "匹配到高危命令：递归强制删除",
            "evidence": "rm -rf ./build/cache",
        },
    }
    started = time.monotonic()
    result = provider.explain(event)
    return {
        "elapsed": round(time.monotonic() - started, 2),
        "degraded": bool(result.degraded),
        "reason": result.reason,
        "model": result.model,
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "text": result.text,
        "detail": result.detail,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表；``None`` 时读 ``sys.argv``。
    :return: 退出码（0 = 全部通过）。
    """
    parser = argparse.ArgumentParser(
        description="NInfer 连接测试：确认哨兵能调到本地 NInfer 并给出人话解释",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help="NInfer 的 API 根地址"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型名")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API 密钥")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="超时秒数（默认 600）"
    )
    parser.add_argument(
        "--max-tokens", type=int, default=1024, help="单次回复上限（默认 1024）"
    )
    parser.add_argument(
        "--nudge", default="", help="给提示加一点扰动（绕过引擎的提示缓存）"
    )
    parser.add_argument(
        "--no-json-mode",
        action="store_true",
        help="不发送 response_format（用来验证引擎认不认这个字段）",
    )
    parser.add_argument("--json", default="", help="把结果写成 JSON 的落盘路径")
    args = parser.parse_args(argv)

    _prepare_stdout()
    _out("=" * 62)
    _out("NInfer 连接测试 —— {}".format(args.base_url))
    _out("=" * 62)

    # --- 1. 模型列表 ---
    _out("")
    _out("[1/3] 问引擎「你是谁」")
    models = probe_models(args.base_url, args.api_key, args.timeout)
    if not models["ok"]:
        _out("  ✗ 引擎没应答：{}".format(models["error"]))
        _out("  → 确认 NInfer 进程在跑、端口对得上。")
        return 2
    for item in models["models"]:
        _out(
            "  ✓ 模型 {}（上下文 {}）".format(
                item.get("id"), item.get("context_window")
            )
        )

    # --- 2. 裸请求 ---
    _out("")
    _out("[2/3] 裸打一次 /chat/completions（看真实响应结构）")
    chat = probe_chat(
        args.base_url,
        args.api_key,
        args.model,
        args.timeout,
        not args.no_json_mode,
        args.max_tokens,
        args.nudge,
    )
    if not chat["ok"]:
        _out("  ✗ 请求失败：{}（耗时 {} 秒）".format(chat["error"], chat["elapsed"]))
        _out("  → 超时就调大 --timeout；模型名要跟 /v1/models 对得上。")
        return 3
    _out("  ✓ HTTP {}，耗时 {} 秒".format(chat["status"], chat["elapsed"]))
    for line in summarize_raw(chat.get("raw")):
        _out("  · {}".format(line))

    # --- 3. 走哨兵自己的通路 ---
    _out("")
    _out("[3/3] 走哨兵自己的 L2 通路（build_provider → explain）")
    provider_result = run_provider(
        args.base_url,
        args.api_key,
        args.model,
        args.timeout,
        args.max_tokens,
        not args.no_json_mode,
    )
    _out(
        "  · 耗时 {} 秒（引擎侧 {} ms）".format(
            provider_result["elapsed"], provider_result["latency_ms"]
        )
    )
    _out(
        "  · provider={} model={} 降级={}".format(
            provider_result["provider"],
            provider_result["model"],
            "是" if provider_result["degraded"] else "否",
        )
    )
    if provider_result["degraded"]:
        _out("  ✗ 降级了：{}".format(provider_result["reason"]))
    _out("")
    _out("  ── 最终落到看板上的那段人话 ──")
    for line in str(provider_result["text"]).splitlines() or [""]:
        _out("  {}".format(line))

    if args.json:
        payload = {
            "base_url": args.base_url,
            "model": args.model,
            "models": models,
            "chat": chat,
            "provider_result": provider_result,
        }
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _out("")
        _out("结果已写入：{}".format(args.json))

    ok = bool(models["ok"] and chat["ok"]) and not provider_result["degraded"]
    _out("")
    _out("结论：{}".format("哨兵能正常调用 NInfer" if ok else "有问题，见上面"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
