"""开箱安全体检 —— 启动时先体检，免得半夜出事才发现。

哨兵要发给别人用。用户拿到 ``sentinel.exe`` 双击就跑，
但他**不知道**自己的环境配好了没有：

* 本地模型（Ollama）开着没？没开的话 L2 解释全靠规则层兜底；
* 云端 API 密钥有效吗？密钥过期了，红线事件就**没有解释**了；
* Webhook 配了吗？**没配的话半夜跑挂了，您将收不到手机推送！**
* 工作目录有 ``.gitignore`` 吗？没有的话快照会把一堆垃圾收进去。

这个模块做四件事，然后把结果**用人话**打印出来。

体检的原则
==========

1. **绝不抛异常** —— 体检失败不该挡住哨兵启动（架构红线 R5）；
2. **说实话** —— 「没配」就说没配，**不假装正常**；
3. **给下一步** —— 每条不通过都要带「怎么办」；
4. **不打扰** —— 已配置且连通的项目只报一句「正常」。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


#: 体检项的状态。
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

#: 状态 → 终端上的记号。
_MARK = {
    STATUS_OK: "[正常]",
    STATUS_WARN: "[警告]",
    STATUS_FAIL: "[失败]",
    STATUS_SKIP: "[跳过]",
}


@dataclass
class HealthItem:
    """一项体检结果。

    :param name: 项目名。
    :param status: 状态（``ok`` / ``warn`` / ``fail`` / ``skip``）。
    :param detail: 人话说明。
    :param hint: 出问题后**该怎么做**。
    :param extra: 附加信息（地址、模型名…）。
    """

    name: str
    status: str = STATUS_OK
    detail: str = ""
    hint: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是不是「完全没问题」。"""
        return self.status == STATUS_OK

    def to_dict(self) -> Dict[str, Any]:
        """转成字典（给 JSON 接口用）。"""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "extra": dict(self.extra),
        }


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------
def _short_error(exc: BaseException) -> str:
    """把异常压成**一句人话**。

    ⚠️ ``requests`` 的 ``ConnectionError`` 消息有一百多字
    （把整个 urllib 追踪都塞进来了）——
    体检报告是给用户看的，不是给排障日志看的。
    """
    kind = type(exc).__name__
    text = "{}: {}".format(kind, exc)
    lowered = text.lower()
    # ⚠️ 先看**异常类型名**再看消息 ——
    # requests 的 ConnectionError 消息里写的是
    # 「Failed to establish a new connection」，压根没「refused」三个字。
    if kind.lower() in ("connectionerror", "connectionrefusederror"):
        return "连不上（服务没启动？）"
    if "connectionrefused" in lowered or "拒绝" in text:
        return "连不上（服务没启动？）"
    if "timeout" in lowered or "timed out" in lowered:
        return "连不上（超时）"
    if "name or service not known" in lowered or "getaddrinfo" in lowered:
        return "连不上（主机名解析失败）"
    if len(text) > 140:
        return "出错了：" + text[:133] + "…"
    return "出错了：" + text


def check_ollama(config: Optional[Dict[str, Any]] = None) -> HealthItem:
    """检查**本地模型**是否连通。

    只发一个**极小**的请求（问它一句话）或一个只读的 ``/models`` ——
    体检不该烧显存、不该让 27B 推理模型跑一轮思考。

    ⚠️ v3.2.1：本地引擎**不只有 Ollama**。
    本机 NInfer（Bonsai2-27B）走的是 OpenAI 兼容协议、地址在 ``127.0.0.1``；
    旧实现只看 ``provider == "ollama"``，
    于是**把本机 NInfer 当成云端跳过了** ——
    体检报告说「跳过」，用户以为没问题，其实引擎可能根本没开。

    :param config: 配置字典。
    :return: 体检结果。
    """
    llm = (config or {}).get("llm") or {}
    provider = str(llm.get("provider") or "ollama").lower()
    if not bool(llm.get("enabled", True)):
        return HealthItem(
            name="本地模型（Ollama）",
            status=STATUS_SKIP,
            detail="模型层已关闭（llm.enabled=false），只用规则层。",
        )
    if provider in ("ollama", "local"):
        return _check_ollama_endpoint(llm)
    if provider in ("openai_compatible", "openai-compatible", "openai"):
        from ..llm.api_client import is_local_endpoint

        base_url = str(llm.get("base_url") or "").strip()
        if not is_local_endpoint(base_url):
            return HealthItem(
                name="本地模型",
                status=STATUS_SKIP,
                detail="当前主力不是本地模型（{}），跳过。".format(provider),
            )
        return _check_local_openai_endpoint(llm)
    return HealthItem(
        name="本地模型（Ollama）",
        status=STATUS_SKIP,
        detail="当前主力不是本地模型（{}），跳过。".format(provider),
    )


def _check_local_openai_endpoint(llm: Dict[str, Any]) -> HealthItem:
    """检查**本机 OpenAI 兼容引擎**（NInfer / 本地 vLLM …）通不通。

    只读 ``GET /models`` —— **不触发任何推理**，
    不烧显存，也不会让 27B 推理模型跑一轮思考（那要 40 秒）。

    :param llm: 配置里的 ``llm`` 段。
    :return: 体检结果。
    """
    base_url = str(llm.get("base_url") or "").strip().rstrip("/")
    model = str(llm.get("model") or "")
    name = "本地模型（{}）".format(model or "OpenAI 兼容引擎")
    try:
        import requests

        response = requests.get(
            "{}/models".format(base_url), timeout=8.0
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            return HealthItem(
                name=name,
                status=STATUS_OK,
                detail="已连通（{} · {}）。".format(base_url, model),
                extra={"base_url": base_url, "model": model},
            )
        return HealthItem(
            name=name,
            status=STATUS_FAIL,
            detail="服务返回 HTTP {}。".format(status),
            hint="确认本机引擎已启动，且端口与 base_url 对得上。",
            extra={"base_url": base_url, "model": model},
        )
    except Exception as exc:  # noqa: BLE001 —— 体检失败只降级
        return HealthItem(
            name=name,
            status=STATUS_FAIL,
            detail="{}。".format(_short_error(exc)),
            hint=(
                "本机引擎没开也能用 —— 哨兵会自动退回规则层判定，"
                "只是没有「人话解释」了。先把引擎拉起来。"
            ),
            extra={"base_url": base_url, "model": model},
        )


def _check_ollama_endpoint(llm: Dict[str, Any]) -> HealthItem:
    """检查本地 Ollama 是否连通（发一个 ``num_predict=8`` 的 ping）。

    :param llm: 配置里的 ``llm`` 段。
    :return: 体检结果。
    """
    from ..llm.ollama_client import DEFAULT_BASE_URL, DEFAULT_MODEL

    base_url = str(llm.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    model = str(llm.get("model") or DEFAULT_MODEL)
    timeout = float(llm.get("timeout") or 20)

    try:
        import requests

        response = requests.post(
            "{}/api/chat".format(base_url),
            json={
                "model": model,
                "stream": False,
                "messages": [{"role": "user", "content": "ping"}],
                "options": {"temperature": 0, "num_predict": 8},
            },
            timeout=min(timeout, 10.0),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            return HealthItem(
                name="本地模型（Ollama）",
                status=STATUS_OK,
                detail="已连通（{} · {}）。".format(base_url, model),
                extra={"base_url": base_url, "model": model},
            )
        if status == 404:
            return HealthItem(
                name="本地模型（Ollama）",
                status=STATUS_FAIL,
                detail="服务在，但模型「{}」不可用。".format(model),
                hint="先装模型：ollama pull {}".format(model),
                extra={"base_url": base_url, "model": model},
            )
        return HealthItem(
            name="本地模型（Ollama）",
            status=STATUS_FAIL,
            detail="服务返回 HTTP {}。".format(status),
            hint="确认 Ollama 已启动：ollama serve",
            extra={"base_url": base_url, "model": model},
        )
    except Exception as exc:  # noqa: BLE001 —— 体检失败只降级
        return HealthItem(
            name="本地模型（Ollama）",
            status=STATUS_FAIL,
            detail="{}。".format(_short_error(exc)),
            hint=(
                "没开 Ollama 也能用 —— 哨兵会自动退回规则层判定，"
                "只是没有「人话解释」了。启动 Ollama：ollama serve"
            ),
            extra={"base_url": base_url, "model": model},
        )


def check_cloud_api(config: Optional[Dict[str, Any]] = None) -> HealthItem:
    """检查云端 API 密钥是否有效。

    发一个**极小**的请求（1 个 token 级别的），只为验密钥。

    :param config: 配置字典。
    :return: 体检结果。
    """
    llm = (config or {}).get("llm") or {}
    provider = str(llm.get("provider") or "ollama").lower()
    if provider not in ("openai_compatible", "openai-compatible", "openai"):
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_SKIP,
            detail="当前主力不是云端模型，跳过。",
        )
    if not bool(llm.get("enabled", True)):
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_SKIP,
            detail="模型层已关闭，跳过。",
        )

    from ..llm.api_client import OpenAICompatibleClient, resolve_api_key

    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_FAIL,
            detail="没有配置 base_url。",
            hint="填上云端模型的 API 地址（例：https://api.deepseek.com/v1）",
        )
    model = str(llm.get("model") or "")
    if not model:
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_FAIL,
            detail="没有配置模型名（llm.model）。",
            hint="填上模型名（例：deepseek-chat）",
        )
    key = resolve_api_key(
        None, base_url, str(llm.get("api_key") or "")
    )
    if not key:
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_FAIL,
            detail="没有找到 API 密钥。",
            hint=(
                "设环境变量 SENTINEL_LLM_API_KEY，"
                "或按厂商设 DEEPSEEK_API_KEY / MOONSHOT_API_KEY 等"
            ),
        )

    try:
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=key,
            model=model,
            timeout=min(float(llm.get("timeout") or 60), 20.0),
            # ⚠️ v3.9.0：``max_tokens=4`` 已被**下限 50** 接管 ——
            #    推理模型会把 4 个 token 全花在思考段上，
            #    正文一个字都吐不出来 ⇒「密钥有效」被误判成「密钥无效」。
            #    ``short_output=True`` 再**强制**注入
            #    ``reasoning_effort="none"`` 并关掉 JSON 模式，
            #    保证这次「说个字」的请求一定能拿到正文。
            short_output=True,
            config=config,
        )
        client.chat("只回答一个字。", "说「好」")
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_OK,
            detail="密钥有效（{} · {}）。".format(base_url, model),
            extra={"base_url": base_url, "model": model},
        )
    except Exception as exc:  # noqa: BLE001 —— 体检失败只降级
        return HealthItem(
            name="云端 API 密钥",
            status=STATUS_FAIL,
            detail="{}。".format(_short_error(exc)),
            hint="检查密钥是否过期、模型名是否正确、余额是否够。",
            extra={"base_url": base_url, "model": model},
        )


def check_webhook(config: Optional[Dict[str, Any]] = None) -> HealthItem:
    """检查有没有配真实 Webhook。

    ⚠️ **没配 Webhook 是一项「警告」而不是「失败」** ——
    哨兵照样能干活，只是半夜出事你**收不到手机推送**。
    但这一点必须**明确告诉用户**，不能默默咽下去。

    :param config: 配置字典。
    :return: 体检结果。
    """
    from .webhook_probe import configured_webhooks

    entries = configured_webhooks(config or {})
    if not entries:
        return HealthItem(
            name="手机推送（Webhook）",
            status=STATUS_WARN,
            detail="没有配置任何真实 Webhook。",
            hint=(
                "⚠️ 如果半夜跑挂，您将收不到手机推送！\n"
                "在 config.yaml 的 alert.channels 里加一段 type: webhook，"
                "或用 test_real_webhook.py 试一次。"
            ),
        )
    urls = [str(item.get("url") or "") for item in entries]
    return HealthItem(
        name="手机推送（Webhook）",
        status=STATUS_OK,
        detail="已配置 {} 个：{}".format(len(urls), "、".join(urls)),
        extra={"urls": urls},
    )


def check_ignore_rules(
    config: Optional[Dict[str, Any]] = None, workdir: str = ""
) -> HealthItem:
    """检查工作目录有没有 ``.gitignore`` / 快照排除规则。

    快照会把工作目录收进去。没有排除规则的话，
    ``node_modules`` / ``.venv`` 这类目录会让快照**又大又慢**。

    :param config: 配置字典。
    :param workdir: 工作目录。
    :return: 体检结果。
    """
    safe_config = (config or {}).get("safe_point") or {}
    target = os.path.abspath(str(workdir or os.getcwd()))
    gitignore = os.path.join(target, ".gitignore")
    has_gitignore = os.path.isfile(gitignore)
    excluded = safe_config.get("excluded_dirs")

    if not os.path.isdir(target):
        return HealthItem(
            name="快照排除规则",
            status=STATUS_SKIP,
            detail="工作目录不存在（{}）。".format(target),
        )
    if excluded:
        return HealthItem(
            name="快照排除规则",
            status=STATUS_OK,
            detail="配置里自定义了排除目录（{} 个）。".format(len(excluded)),
            extra={"source": "config"},
        )
    if has_gitignore:
        return HealthItem(
            name="快照排除规则",
            status=STATUS_OK,
            detail="工作目录有 .gitignore；快照另有一份默认排除清单。",
            extra={"source": "gitignore"},
        )
    return HealthItem(
        name="快照排除规则",
        status=STATUS_WARN,
        detail="工作目录没有 .gitignore。",
        hint=(
            "快照用的是内置排除清单（.git / node_modules / .venv 等），"
            "一般够用；但如果你有大目录，加个 .gitignore 更保险。"
        ),
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def run_health_check(
    config: Optional[Dict[str, Any]] = None,
    *,
    workdir: str = "",
    skip_network: bool = False,
) -> List[HealthItem]:
    """跑完整套体检。

    **绝不抛异常** —— 体检失败不该挡住哨兵启动（架构红线 R5）。

    :param config: 配置字典。
    :param workdir: 工作目录。
    :param skip_network: 跳过要发网络请求的两项（快速模式）。
    :return: 体检结果列表。
    """
    items: List[HealthItem] = []
    if skip_network:
        items.append(
            HealthItem(
                name="本地模型（Ollama）",
                status=STATUS_SKIP,
                detail="已跳过网络检查。",
            )
        )
        items.append(
            HealthItem(
                name="云端 API 密钥",
                status=STATUS_SKIP,
                detail="已跳过网络检查。",
            )
        )
    else:
        try:
            items.append(check_ollama(config))
        except Exception as exc:  # noqa: BLE001 —— 体检失败只降级
            items.append(
                HealthItem(
                    name="本地模型（Ollama）",
                    status=STATUS_SKIP,
                    detail="检查异常：{}: {}".format(type(exc).__name__, exc),
                )
            )
        try:
            items.append(check_cloud_api(config))
        except Exception as exc:  # noqa: BLE001
            items.append(
                HealthItem(
                    name="云端 API 密钥",
                    status=STATUS_SKIP,
                    detail="检查异常：{}: {}".format(type(exc).__name__, exc),
                )
            )
    try:
        items.append(check_webhook(config))
    except Exception as exc:  # noqa: BLE001
        items.append(
            HealthItem(
                name="手机推送（Webhook）",
                status=STATUS_SKIP,
                detail="检查异常：{}: {}".format(type(exc).__name__, exc),
            )
        )
    try:
        items.append(check_ignore_rules(config, workdir))
    except Exception as exc:  # noqa: BLE001
        items.append(
            HealthItem(
                name="快照排除规则",
                status=STATUS_SKIP,
                detail="检查异常：{}: {}".format(type(exc).__name__, exc),
            )
        )
    return items


def summarize(items: List[HealthItem]) -> Dict[str, Any]:
    """把体检结果汇总成一句话 + 计数。

    :param items: 体检结果列表。
    :return: ``{"counts": {...}, "verdict": "..."}``。
    """
    counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_SKIP: 0}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    if counts.get(STATUS_FAIL):
        verdict = (
            "有 {} 项体检没通过 —— 建议先处理，否则半夜出问题你不会知道。"
            .format(counts[STATUS_FAIL])
        )
    elif counts.get(STATUS_WARN):
        verdict = (
            "基本就绪，但有 {} 项值得留意（见上方提示）。"
            .format(counts[STATUS_WARN])
        )
    else:
        verdict = "全部就绪 —— 可以放心挂机。"
    return {"counts": counts, "verdict": verdict}


def _engine_failure_banner(items: List[HealthItem]) -> str:
    """本地引擎连不上时，给一段**极度清晰**的中文提示（v3.3.0）。

    为什么单独搞一段
    ===============

    体检报告是「表格」—— 一行一项，用户很容易扫过去。
    但「本地模型连不上」这件事的后果**很重**：
    L2 判定与人话解释**整个停用**，用户看到的只剩干巴巴的规则告警，
    很容易误以为「哨兵坏了」。

    所以这一段**跳出表格**，用最直白的话讲清楚三件事：
    **现在会怎样 / 怎么办 / 怎么自查**。

    :param items: 体检结果列表。
    :return: 多行文本；没有引擎故障时返回空串。
    """
    broken = [
        item
        for item in items
        if item.status == STATUS_FAIL
        and (item.extra or {}).get("base_url")
    ]
    if not broken:
        return ""
    item = broken[0]
    base_url = str((item.extra or {}).get("base_url") or "")
    model = str((item.extra or {}).get("model") or "")
    lines = [
        "",
        "!" * 62,
        "  严重：本地推理引擎连不上！L2 判定与人话解释**已停用**",
        "!" * 62,
        "  引擎　：{}{}".format(
            model, "（{}）".format(base_url) if base_url else ""
        ),
        "  原因　：{}".format(item.detail),
        "",
        "  现在会怎样：",
        "    · 规则层（L1）**照常工作** —— 删文件、越权操作照样红色告警；",
        "    · 但**没有「人话解释」**了 —— 你只能看到原始命令与规则原因；",
        "    · 引擎恢复后会自动接上（v3.3.0 起哨兵会**用心跳盯着**它）。",
        "",
        "  怎么办：",
        "    · 先把本地引擎拉起来（本机 NInfer：双击引擎目录里的启动面板）；",
        "    · 或者把 llm.base_url 改成一个**确实在跑**的模型服务；",
        "    · 自查：浏览器打开 {}/models，能出一段 JSON 就是好的。".format(
            base_url or "http://127.0.0.1:8888/v1"
        ),
        "!" * 62,
    ]
    return "\n".join(lines)


def render_terminal(items: List[HealthItem]) -> str:
    """把体检结果渲染成终端报告。

    :param items: 体检结果列表。
    :return: 多行文本。
    """
    lines = [
        "=" * 62,
        "  哨兵安全体检报告",
        "=" * 62,
    ]
    for item in items:
        lines.append(
            "{} {}".format(_MARK.get(item.status, "[?]"), item.name)
        )
        lines.append("       {}".format(item.detail))
        if item.hint and item.status != STATUS_OK:
            # ⚠️ hint 里可能带换行；每行**统一 strip** 后再缩进，
            # 否则不同来源的提示缩进会歪成锯齿状。
            for line in item.hint.splitlines():
                text = line.strip()
                if not text:
                    continue
                lines.append("       {}".format(text))
    summary = summarize(items)
    lines.append("-" * 62)
    lines.append(
        "正常 {} · 警告 {} · 失败 {} · 跳过 {}".format(
            summary["counts"][STATUS_OK],
            summary["counts"][STATUS_WARN],
            summary["counts"][STATUS_FAIL],
            summary["counts"][STATUS_SKIP],
        )
    )
    lines.append(summary["verdict"])
    lines.append("=" * 62)
    banner = _engine_failure_banner(items)
    if banner:
        lines.append(banner)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 启动体检（一个进程只跑一次）
# ---------------------------------------------------------------------------

#: 进程内「已经体检过」的标记。
#:
#: 为什么需要这个守卫
#: ==================
#:
#: 一次启动里**可能**有不止一个地方想体检：
#: exe 入口（:mod:`sentinel_entry`）、看板的演示模式
#: （:mod:`src.gui.server`）、真实接管启动器（``run_with_sentinel.py``）。
#:
#: 它们各自都合理，但**都打印一遍**的话，用户会看到
#: 两份统计（还可能不一样 —— 因为检查的工作目录不同），
#: 然后以为哨兵出了两次问题。
#:
#: 所以：**一个进程只跑一次、只打印一次**。
_STARTUP_DONE = False

#: 已经跑出来的体检结果（第二次调用直接返回它）。
_STARTUP_ITEMS: List[HealthItem] = []


def reset_startup_health_cache() -> None:
    """清掉「已经体检过」的标记。

    只给**测试**用 —— 进程内跑多个用例时，
    前一个用例的标记会挡住后一个。

    :return: 无。
    """
    global _STARTUP_DONE, _STARTUP_ITEMS
    _STARTUP_DONE = False
    _STARTUP_ITEMS = []


def run_startup_health_check(
    config: Optional[Dict[str, Any]] = None,
    *,
    workdir: str = "",
    skip_network: bool = False,
    printer: Optional[Any] = None,
) -> List[HealthItem]:
    """启动时跑一次体检并打印报告 —— **一个进程只跑一次**。

    ⚠️ 重复调用**不会**再打印报告（也不会再发网络请求），
    直接返回第一次的结果。

    :param config: 哨兵配置。
    :param workdir: 被体检的工作目录。
    :param skip_network: 跳过要发网络请求的两项。
    :param printer: 打印回调；``None`` 时用 :func:`safe_print`。
    :return: 体检结果列表。
    """
    global _STARTUP_DONE, _STARTUP_ITEMS
    if _STARTUP_DONE:
        return list(_STARTUP_ITEMS)
    items = run_health_check(
        config, workdir=workdir, skip_network=skip_network
    )
    _STARTUP_ITEMS = list(items)
    # ⚠️ 先置位再打印 —— 万一打印本身炸了，
    # 也不能让下一次调用再试一遍（那就又变成两份报告了）。
    _STARTUP_DONE = True
    text = render_terminal(items)
    if printer is not None:
        try:
            printer(text)
        except Exception as exc:  # noqa: BLE001 —— 打印失败不阻断启动
            logger.warning("打印体检报告失败：%s: %s", type(exc).__name__, exc)
    else:
        safe_print(text)
    return items


def safe_print(text: str) -> None:
    """把体检报告打到终端 —— 编码装不下时**降级**，不炸。

    报告里有个 ``⚠️``。Windows 控制台本身是 UTF-8 的，没问题；
    但要是 stdout 被重定向到只支持 GBK 的环境，
    ``print`` 会抛 :class:`UnicodeEncodeError` ——
    **体检报告把哨兵启动搞崩了**，那就本末倒置了。

    :param text: 要打印的文本。
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(text.encode(encoding, "replace").decode(encoding), flush=True)
        except (UnicodeError, LookupError, ValueError):
            print("(体检报告里有当前控制台显示不了的字符，已略过)", flush=True)


def render_markdown(items: List[HealthItem]) -> str:
    """把体检结果渲染成 Markdown（给 GUI 面板用）。

    :param items: 体检结果列表。
    :return: Markdown 文本。
    """
    summary = summarize(items)
    lines = [
        "## 哨兵安全体检报告",
        "",
        summary["verdict"],
        "",
        "| 项目 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    for item in items:
        label = {
            STATUS_OK: "✅ 正常",
            STATUS_WARN: "⚠️ 警告",
            STATUS_FAIL: "❌ 失败",
            STATUS_SKIP: "－ 跳过",
        }.get(item.status, item.status)
        detail = item.detail.replace("|", "\\|")
        if item.hint and item.status != STATUS_OK:
            hint_lines = [
                line.strip() for line in item.hint.splitlines() if line.strip()
            ]
            if hint_lines:
                detail += "<br>" + "<br>".join(hint_lines)
        lines.append("| {} | {} | {} |".format(item.name, label, detail))
    lines.append("")
    return "\n".join(lines)
