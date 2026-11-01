"""本地推理引擎的**心跳看门狗**（v3.3.0）。

为什么需要它
============

v3.2.1 实机踩出来的坑：**本机 NInfer 引擎会掉线**。

而且掉线的方式很讨厌 —— 它**不报错**，只是**不说话了**：

* 引擎进程可能直接退出（显存吃紧、模型文件被占用、被人手滑关掉）；
* 也可能端口还在监听，但内部已经卡死。

对哨兵来说，引擎掉线意味着**整个 L2 层静默失效**：
规则层（L1）还在跑，告警还在出，但**没有「人话解释」了**，
用户看到的是一堆干巴巴的 ``[CRITICAL] DANGEROUS: ...``，
根本不知道 Agent 到底干了什么 —— 以为哨兵坏了。

所以这一版让哨兵**不只是审计员，还是引擎的保姆**：

* 每 30 秒发一个**极简只读**请求（``GET /models``）探活；
* 掉线了立刻在**终端**和**看板**发红色警告，
  并写进告警日志（走的是和其他告警**同一条**通道，
  所以配了 Webhook 的用户**手机上也会响**）；
* 引擎回来了也要报一声 —— 否则用户不知道哨兵又能解释了。

三条硬约束
==========

1. **只读**：探活只用 ``GET /models`` —— **不触发任何推理**，
   不烧显存，也不会让 27B 推理模型跑一轮思考（那要 40 秒）。
2. **不撞车**：NInfer 是 ``max-concurrency 1``，同时两个请求会**直接崩引擎**。
   所以探活前会**非阻塞地**抢一下那台引擎的串行锁：
   抢不到就**跳过这一拍**（引擎正在推理，说明它活着）。
3. **绝不抛异常**：审查侧的任何异常都只降级（架构红线 R5）。

自动重拉
========

⚠️ **v3.3.0 砍掉了自动重拉。**

初稿里有「掉线后按配置里的命令自动拉起引擎」，
但用户的要求是明确的：「**不要尝试自动拉起，保持只读监控的边界**」。

哨兵**替用户启动一个进程**就不再是只读监控了；
而且引擎启动要吃显存 —— 在用户正被别的东西占满显存的时候自动拉它，
只会把机器拖得更垮。

所以现在只**说**（「引擎掉了，你手动拉一下」），**不动手**。
配置里的 ``llm.watchdog.restart`` 段会被**忽略**（并记一条日志提醒）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_OFFLINE_ALERT_REASON",
    "ENGINE_RECOVERED_ALERT_REASON",
    "VERDICT_ENGINE_OFFLINE",
    "VERDICT_ENGINE_RECOVERED",
    "EngineWatchdog",
    "start_engine_watchdog",
    "stop_engine_watchdog",
]

#: 引擎掉线的判定名。
VERDICT_ENGINE_OFFLINE = "ENGINE_OFFLINE"

#: 引擎恢复的判定名。
VERDICT_ENGINE_RECOVERED = "ENGINE_RECOVERED"

#: 引擎掉线时的固定告警文案。
#:
#: ⚠️ 这句话是**用户可见**的承诺 —— 哨兵靠它告诉用户
#: 「L2 已经停用了」，别让用户对着干巴巴的告警猜引擎是不是坏了。
ENGINE_OFFLINE_ALERT_REASON = (
    "[CRITICAL] NInfer 引擎已掉线！L2 判定和解释已停用！"
)

#: 引擎恢复时的告警文案。
ENGINE_RECOVERED_ALERT_REASON = (
    "[OK] NInfer 引擎已恢复！L2 判定和解释重新可用。"
)

#: 探活状态。
STATE_UNKNOWN = "unknown"
STATE_HEALTHY = "healthy"
STATE_OFFLINE = "offline"

#: 默认探活间隔（秒）。
DEFAULT_INTERVAL = 30.0

#: 默认探活超时（秒）。
DEFAULT_TIMEOUT = 8.0

#: 进程内按 ``base_url`` 去重的看门狗表。
_WATCHDOGS: Dict[str, "EngineWatchdog"] = {}

#: 保护上面那张表。
_WATCHDOG_GUARD = threading.Lock()


def _short_error(exc: BaseException) -> str:
    """把异常压成**一句人话**（跟体检模块一个口径）。"""
    kind = type(exc).__name__
    text = "{}: {}".format(kind, exc)
    lowered = text.lower()
    if kind.lower() in ("connectionerror", "connectionrefusederror"):
        return "连不上（服务没启动？）"
    if "connectionrefused" in lowered or "拒绝" in text:
        return "连不上（服务没启动？）"
    if "timeout" in lowered or "timed out" in lowered:
        return "连不上（超时）"
    if len(text) > 120:
        return "出错了：" + text[:113] + "…"
    return "出错了：" + text


class EngineWatchdog:
    """本机推理引擎的**心跳看门狗**。

    :param base_url: 引擎根地址（``http://127.0.0.1:8888/v1``）。
    :param model: 模型名（写进告警，方便用户核对）。
    :param interval: 探活间隔（秒）。
    :param timeout: 单次探活超时（秒）。
    :param provider_name: 引擎的显示名（默认「本地推理引擎」）。
    :param restart: 自动重拉配置（``{"enabled": bool, "command": [...],
        "cwd": str, "max_attempts": int, "cooldown_seconds": float}``）。
    :param prober: 探活函数（测试用；默认走只读 ``GET /models``）。
    :param clock: 时钟（测试用）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "",
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        provider_name: str = "",
        restart: Optional[Dict[str, Any]] = None,
        prober: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.model = str(model or "").strip()
        self.interval = max(float(interval or DEFAULT_INTERVAL), 1.0)
        self.timeout = max(float(timeout or DEFAULT_TIMEOUT), 0.5)
        self.provider_name = str(provider_name or "本地推理引擎")
        self.restart = dict(restart or {})
        self._prober = prober
        self._clock = clock

        #: 探活回调（收到告警字典）。
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        #: 当前状态。
        self.state = STATE_UNKNOWN
        #: 最近一次探活的说明。
        self.detail = "还没探过。"
        #: 最近一次探活的时间戳。
        self.checked_at = 0.0
        #: 最近一次探活的耗时（毫秒）。
        self.latency_ms = 0.0
        #: 累计探活次数 / 失败次数。
        self.checks = 0
        self.failures = 0
        #: 自动重拉的尝试次数（v3.3.0 起**恒为 0** —— 不再自动拉起）。
        self.restart_attempts = 0
        #: 线程与停止信号。
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def add_listener(
        self, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """登记一个告警回调。

        :param callback: 收到告警字典时调用。
        :return: 无。
        """
        if callback is None:
            return
        with self._lock:
            self._listeners.append(callback)

    def start(self) -> bool:
        """起后台线程。**幂等** —— 已经在跑就直接返回。

        :return: 线程是否由本次调用启动。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="sentinel-engine-watchdog",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """停掉后台线程。

        :param timeout: 等待线程退出的最长时间（秒）。
        :return: 无。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def probe(self) -> Dict[str, Any]:
        """探一次活，返回结果字典（**不**发告警）。

        探测期间会**非阻塞地**抢引擎的串行锁 ——
        抢不到说明引擎正在推理，直接跳过这一拍，
        绝不与正在跑的推理请求抢单并发（会把引擎撞崩）。

        :return: ``{"ok": bool, "detail": str, "latency_ms": float}``。
        """
        if not self.base_url:
            return {"ok": False, "detail": "没配置 base_url", "latency_ms": 0.0}
        lock = self._engine_lock()
        if lock is not None and not lock.acquire(blocking=False):
            # 引擎正在忙 —— 说明它**活着**（有人在用它）。
            return {
                "ok": True,
                "detail": "引擎正在处理请求，本拍跳过。",
                "latency_ms": 0.0,
                "skipped": True,
            }
        started = self._clock()
        try:
            if self._prober is not None:
                result = self._prober()
                ok = bool(result.get("ok"))
                detail = str(result.get("detail") or "")
            else:
                ok, detail = self._http_probe()
        except Exception as exc:  # noqa: BLE001 —— 探活失败只降级
            ok = False
            detail = _short_error(exc)
        finally:
            if lock is not None:
                lock.release()
        elapsed = max((self._clock() - started) * 1000.0, 0.0)
        return {"ok": ok, "detail": detail, "latency_ms": elapsed}

    def check_once(self) -> Dict[str, Any]:
        """探一次活，并按状态迁移**发告警**。

        :return: 本次探活的结果字典。
        """
        result = self.probe()
        ok = bool(result.get("ok"))
        detail = str(result.get("detail") or "")
        with self._lock:
            previous = self.state
            self.checks += 1
            self.checked_at = self._clock()
            self.latency_ms = float(result.get("latency_ms") or 0.0)
            if not ok:
                self.failures += 1
            self.detail = detail or ("正常" if ok else "连不上")
            # 第一次探活：**也**要告警 ——
            # 用户可能是在引擎还没起来时就启动了哨兵，
            # 那他必须立刻知道「L2 现在是停用的」。
            if previous == STATE_UNKNOWN:
                self.state = STATE_HEALTHY if ok else STATE_OFFLINE
                if not ok:
                    self._emit(self._offline_alert(detail))
                    self._try_restart()
                return result
            if ok and previous == STATE_OFFLINE:
                self.state = STATE_HEALTHY
                self._emit(self._recovery_alert(detail))
            elif not ok and previous == STATE_HEALTHY:
                self.state = STATE_OFFLINE
                self._emit(self._offline_alert(detail))
                self._try_restart()
        return result

    def status(self) -> Dict[str, Any]:
        """返回当前状态的快照（给看板用）。

        :return: 状态字典。
        """
        with self._lock:
            return {
                "base_url": self.base_url,
                "model": self.model,
                "provider_name": self.provider_name,
                "state": self.state,
                "detail": self.detail,
                "checked_at": self.checked_at,
                "latency_ms": self.latency_ms,
                "checks": self.checks,
                "failures": self.failures,
                "interval": self.interval,
                "running": bool(
                    self._thread is not None and self._thread.is_alive()
                ),
                "restart_attempts": self.restart_attempts,
            }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        """后台线程主体：每隔 ``interval`` 探一次活。"""
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 —— 线程绝不能死
                logger.warning(
                    "引擎心跳检查异常，已跳过：%s: %s",
                    type(exc).__name__,
                    exc,
                )
            self._stop.wait(self.interval)

    def _engine_lock(self) -> Optional[threading.Lock]:
        """拿到这台引擎的**串行锁**（拿不到就返回 ``None``）。"""
        try:
            from ..llm.base import engine_lock_for

            return engine_lock_for(self.base_url)
        except Exception as exc:  # noqa: BLE001 —— 拿不到锁就裸探
            logger.debug(
                "拿不到引擎串行锁，按无锁探测：%s: %s",
                type(exc).__name__,
                exc,
            )
            return None

    def _http_probe(self) -> Any:
        """只读探活：``GET {base_url}/models``。

        :return: ``(是否通, 人话说明)``。
        """
        url = "{}/models".format(self.base_url)
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                resp.read(4096)
            if 200 <= status < 300:
                return True, "已连通（HTTP {}）。".format(status)
            return False, "服务返回 HTTP {}。".format(status)
        except urllib.error.HTTPError as exc:
            return False, "服务返回 HTTP {}。".format(int(exc.code))
        except urllib.error.URLError as exc:
            return False, _short_error(exc)
        except OSError as exc:
            return False, _short_error(exc)

    def _offline_alert(self, detail: str) -> Dict[str, Any]:
        """构造「引擎掉线」告警字典。

        :param detail: 探活失败的原因。
        :return: 告警字典。
        """
        reason = "{}（地址 {}，最近一次心跳：{}）".format(
            ENGINE_OFFLINE_ALERT_REASON, self.base_url, detail or "连不上"
        )
        return self._base_alert(
            reason=reason,
            level="critical",
            verdict=VERDICT_ENGINE_OFFLINE,
            rule_id="engine_offline",
            evidence=detail,
        )

    def _recovery_alert(self, detail: str) -> Dict[str, Any]:
        """构造「引擎恢复」告警字典。

        :param detail: 探活成功的原因。
        :return: 告警字典。
        """
        reason = "{}（地址 {}，探活耗时 {} 毫秒）".format(
            ENGINE_RECOVERED_ALERT_REASON,
            self.base_url,
            int(self.latency_ms),
        )
        return self._base_alert(
            reason=reason,
            level="info",
            verdict=VERDICT_ENGINE_RECOVERED,
            rule_id="engine_recovered",
            evidence=detail,
        )

    def _base_alert(
        self,
        *,
        reason: str,
        level: str,
        verdict: str,
        rule_id: str,
        evidence: str,
    ) -> Dict[str, Any]:
        """告警字典的公共骨架。

        :param reason: 告警正文。
        :param level: 等级（``critical`` / ``info``）。
        :param verdict: 判定名。
        :param rule_id: 规则 ID。
        :param evidence: 证据。
        :return: 告警字典。
        """
        return {
            "alert_id": "engine-{}".format(int(self._clock() * 1000)),
            "timestamp": self._clock(),
            "verdict": verdict,
            "level": level,
            "rule_id": rule_id,
            "reason": reason,
            "evidence": str(evidence or ""),
            "severity": "standard",
            "category": "engine_health",
            "source": "engine_watchdog",
            "event_type": "engine_health",
            "tool": "",
            "command": "",
            "arguments": {},
            "agent": "",
            "task_goal": "",
            "triggers": ["engine_offline"],
            "token_usage": {},
        }

    def _emit(self, alert: Dict[str, Any]) -> None:
        """把告警发给所有回调。

        :param alert: 告警字典。
        :return: 无。
        """
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(alert)
            except Exception as exc:  # noqa: BLE001 —— 回调失败不影响看门狗
                logger.warning(
                    "引擎看门狗告警回调失败：%s: %s",
                    type(exc).__name__,
                    exc,
                )

    def _try_restart(self) -> None:
        """⚠️ v3.3.0：**这一版不自动拉起引擎**（保留占位，仅记日志）。

        为什么砍掉自动重拉
        =================

        v3.3.0 初稿里有「掉线后按配置里的命令自动重拉引擎」。
        但用户的要求是明确的：
        「**不要尝试自动拉起，保持只读监控的边界**」。

        理由也站得住：
        自动拉起意味着哨兵**替用户启动了一个进程** ——
        那已经不是只读监控了；而且引擎启动要吃显存，
        在用户正被别的东西占满显存的时候自动拉它，
        只会把机器拖得更垮。

        所以现在只**说**（「引擎掉了，你手动拉一下」），
        **不动手**。真要自己拉，用户自己知道怎么拉。

        :return: 无。
        """
        restart = self.restart
        if not bool(restart.get("enabled")):
            return
        logger.warning(
            "配置里仍然写着 llm.watchdog.restart.enabled = true，"
            "但 v3.3.0 **不再自动拉起引擎**（只读监控边界）—— "
            "该配置项已被忽略，请手动拉起引擎。"
        )


# ---------------------------------------------------------------------------
# 进程级单例（同一台引擎只起一个看门狗）
# ---------------------------------------------------------------------------

def _watchdog_key(config: Dict[str, Any]) -> str:
    """从配置算出看门狗的**去重键**（引擎地址）。

    :param config: 哨兵配置。
    :return: 键字符串。
    """
    llm = (config or {}).get("llm") or {}
    base_url = str(llm.get("base_url") or "").strip().rstrip("/").lower()
    return base_url or "__none__"


def build_watchdog(config: Dict[str, Any]) -> Optional[EngineWatchdog]:
    """按配置造一个看门狗（**不**启动线程）。

    ⚠️ **只盯本机引擎** ——
    云端 API 掉线是**服务商**的事，哨兵管不着（也不该替它喊）；
    更重要的是，哨兵**不该**定时去戳第三方的服务器。
    所以这里只认 ``127.0.0.1`` / ``localhost`` 这类本机地址。

    :param config: 哨兵配置。
    :return: 看门狗；不该建时返回 ``None``。
    """
    llm = (config or {}).get("llm") or {}
    if not bool(llm.get("enabled", True)):
        return None
    watchdog_config = llm.get("watchdog") or {}
    if not bool(watchdog_config.get("enabled", False)):
        return None
    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        return None
    try:
        from ..llm.api_client import is_local_endpoint

        if not is_local_endpoint(base_url):
            logger.info(
                "模型层不是本机地址（%s），不启用引擎看门狗。", base_url
            )
            return None
    except Exception as exc:  # noqa: BLE001 —— 判不出来就按「不是本机」处理
        logger.info("无法判断模型层地址是否本机，不启用引擎看门狗：%s", exc)
        return None
    provider = str(llm.get("provider") or "").lower()
    name = "本地推理引擎"
    if provider in ("openai_compatible", "openai-compatible", "openai"):
        name = "NInfer 引擎" if "8888" in base_url else "本地模型引擎"
    return EngineWatchdog(
        base_url,
        model=str(llm.get("model") or ""),
        interval=float(watchdog_config.get("interval") or DEFAULT_INTERVAL),
        timeout=float(watchdog_config.get("timeout") or DEFAULT_TIMEOUT),
        provider_name=name,
        restart=dict(watchdog_config.get("restart") or {}),
    )


def start_engine_watchdog(
    config: Dict[str, Any],
    on_alert: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Optional[EngineWatchdog]:
    """按配置起看门狗（**同地址进程内只起一个**）。

    :param config: 哨兵配置。
    :param on_alert: 告警回调。
    :return: 看门狗；没起时返回 ``None``。
    """
    watchdog = build_watchdog(config)
    if watchdog is None:
        return None
    key = _watchdog_key(config)
    with _WATCHDOG_GUARD:
        existing = _WATCHDOGS.get(key)
        if existing is not None:
            if on_alert is not None:
                existing.add_listener(on_alert)
            existing.start()
            return existing
        _WATCHDOGS[key] = watchdog
    if on_alert is not None:
        watchdog.add_listener(on_alert)
    watchdog.start()
    return watchdog


def stop_engine_watchdog(config: Dict[str, Any]) -> None:
    """按配置停掉看门狗（收尾用）。

    :param config: 哨兵配置。
    :return: 无。
    """
    key = _watchdog_key(config)
    with _WATCHDOG_GUARD:
        watchdog = _WATCHDOGS.pop(key, None)
    if watchdog is not None:
        watchdog.stop()


def watchdog_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """查某个地址的看门狗状态（给看板用）。

    :param config: 哨兵配置。
    :return: 状态字典；没有看门狗时返回空状态。
    """
    key = _watchdog_key(config)
    with _WATCHDOG_GUARD:
        watchdog = _WATCHDOGS.get(key)
    if watchdog is None:
        return {
            "state": STATE_UNKNOWN,
            "detail": "看门狗未启用。",
            "running": False,
        }
    return watchdog.status()


def dumps_status(status: Dict[str, Any]) -> str:
    """把状态渲染成一行 JSON（调试用）。

    :param status: 状态字典。
    :return: JSON 字符串。
    """
    return json.dumps(status, ensure_ascii=False)
