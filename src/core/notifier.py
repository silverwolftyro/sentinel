"""告警分发层 —— 把一条告警按配置分发到多个通道。

职责边界
========
哨兵的核心流程（:mod:`src.core.engine`）只负责**发现**异常；
本模块负责**通知**：读 ``alert`` 配置段，把告警分发给各通道。

三条硬约束
==========
1. **不抛异常** —— 任何通道失败都被隔离成一条 warning，绝不让
   告警通道的问题反过来打断被监控 Agent（红线 R5：旁路失败）。
2. **等级过滤** —— 低于 ``alert.min_level`` 的告警一律不投递，
   避免低价值告警把桌面弹窗刷爆。
3. **未知配置不致命** —— 未知的通道类型只记 warning 并跳过，
   配置写错不应该让哨兵起不来。

配置示例（``config/config.yaml``）::

    alert:
      enabled: true
      min_level: "info"          # info < warn < critical
      channels:
        - type: "console"
          enabled: true
        - type: "file"
          enabled: true
          path: "logs/sentinel_alert.log"
        - type: "desktop"
          enabled: true
          title: "哨兵告警"
        - type: "sound"
          enabled: true
        - type: "webhook"
          enabled: false
          url: ""
          timeout: 10
          format: "dingtalk"     # dingtalk | feishu | serverchan | bark | generic
          card: false            # 发 Markdown 卡片而不是纯文本
          secret: ""             # 钉钉 / 飞书的加签密钥
          triggers: []           # 只推这些触发类型；留空 = 推全部
          dedup_seconds: 60      # 防骚扰：同一条事件 60 秒内不重复推
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config_loader import load_config
from .channels import (
    DEFAULT_DEDUP_SECONDS,
    DEFAULT_DESKTOP_TITLE,
    DEFAULT_FILE_CHANNEL_PATH,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_TIMEOUT,
    BaseChannel,
    ConsoleChannel,
    DesktopChannel,
    FileChannel,
    SoundChannel,
    WebhookChannel,
    alert_level,
    format_alert,
    level_rank,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MERGE_SECONDS",
    "DEFAULT_MIN_LEVEL",
    "AlertCoalescer",
    "Notifier",
]

#: 缺省最低告警等级。
DEFAULT_MIN_LEVEL = "info"

#: 缺省合并窗口（秒）。
#:
#: 窗口内**同一件事**（Agent + 规则 + 命令相同）的重复告警
#: 只会敲门一次，次数记下来，等下一次推送时一并告诉用户。
DEFAULT_MERGE_SECONDS = 60.0


class AlertCoalescer:
    """告警合并器 —— 防「半夜被同一条告警敲 5 次门」。

    为什么需要它
    ============

    v1.x 只有 webhook 通道带指纹去重，**桌面弹窗和声音没有**。
    而 Agent 卡死时最典型的形态就是：它反复跑同一条命令、
    反复失败、反复触发同一个规则 —— 5 条一模一样的 STUCK。

    如果每条都弹一次窗，用户在半夜会被叫醒 5 次，
    第二次之后他就会把哨兵静音 —— 那才是真正的损失。

    合并规则
    =======

    * 指纹 = ``Agent + 规则 ID + 命令``（与 webhook 一致）；
    * 窗口内的重复 → **不推**，但**计数**；
    * 下一次真正推送时，把计数附在告警上
      （``merged_count``），用户一眼知道「刚才那 3 条已合并」；
    * **红线级告警永远直接推** —— 合并绝不能盖住真正要紧的事。

    :param window_seconds: 合并窗口（秒）；``0`` 表示不合并。
    """

    def __init__(self, window_seconds: float = DEFAULT_MERGE_SECONDS) -> None:
        self.window_seconds = max(float(window_seconds or 0.0), 0.0)
        #: 指纹 → 上次**真正推送**的时刻。
        self._last_pushed: Dict[str, float] = {}
        #: 指纹 → 已合并但还没告知用户的条数。
        self._merged: Dict[str, int] = {}
        #: 指纹 → 最近一条同指纹的告警（摘要要用）。
        self._last_alert: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def fingerprint(alert: Dict[str, Any]) -> str:
        """算这条告警的指纹。

        :param alert: 告警字典。
        :return: 指纹字符串。
        """
        data = alert if isinstance(alert, dict) else {}
        parts = [
            str(data.get("agent") or ""),
            str(data.get("rule_id") or ""),
            str(data.get("command") or ""),
        ]
        return "|".join(parts)

    @staticmethod
    def is_red_line(alert: Dict[str, Any]) -> bool:
        """是不是红线级告警。"""
        data = alert if isinstance(alert, dict) else {}
        if str(data.get("severity") or "") == "red_line":
            return True
        return str(data.get("verdict") or "").upper() == "DANGEROUS"

    def decide(self, alert: Dict[str, Any]) -> Tuple[str, int]:
        """决定这条告警要不要**真的**推出去。

        :param alert: 告警字典。
        :return: ``("send", 已合并条数)`` 或 ``("merge", 0)``。
        """
        if self.window_seconds <= 0:
            return "send", 0
        if self.is_red_line(alert):
            # 红线永远直接推 —— 合并绝不能盖住要紧的事。
            return "send", 0
        key = self.fingerprint(alert)
        now = time.monotonic()
        with self._lock:
            self._last_alert[key] = dict(alert)
            last = self._last_pushed.get(key)
            if last is not None and (now - last) < self.window_seconds:
                self._merged[key] = self._merged.get(key, 0) + 1
                return "merge", 0
            self._last_pushed[key] = now
            merged = self._merged.pop(key, 0)
        return "send", merged

    def expired_summaries(self) -> List[Dict[str, Any]]:
        """取出**窗口已过**的合并摘要。

        为什么需要它
        ============

        合并有个副作用：如果 Agent 连续卡了 5 次然后**停了**，
        那 5 条里只有第 1 条被推出去，而「其实发生了 5 次」
        这个信息会一直烂在计数里 —— 用户永远看不到。

        所以窗口一过，就把攒下的条数**补一条摘要**推出去：
        *「同类告警已合并 N 条」*。

        合并不是隐瞒 —— 只是别一次敲 5 次门。

        :return: 摘要告警列表（可能为空）。
        """
        if self.window_seconds <= 0:
            return []
        now = time.monotonic()
        summaries: List[Dict[str, Any]] = []
        with self._lock:
            for key in list(self._merged.keys()):
                last = self._last_pushed.get(key)
                if last is None or (now - last) < self.window_seconds:
                    continue
                count = self._merged.pop(key, 0)
                if count <= 0:
                    continue
                base = dict(self._last_alert.get(key) or {})
                base["merged_count"] = count
                base["merged_summary"] = True
                base["reason"] = (
                    "{reason}（同类告警在 {seconds} 秒内合并了 {count} 条）"
                ).format(
                    reason=str(base.get("reason") or "同类告警"),
                    seconds=int(self.window_seconds),
                    count=count,
                )
                summaries.append(base)
        return summaries

    def stats(self) -> Dict[str, int]:
        """当前合并统计（测试与调试用）。"""
        with self._lock:
            return {
                "tracked": len(self._last_pushed),
                "merged": sum(self._merged.values()),
            }


def _build_console(entry: Dict[str, Any]) -> BaseChannel:
    """构造终端通道。"""
    return ConsoleChannel()


def _build_file(entry: Dict[str, Any]) -> BaseChannel:
    """构造文件通道。"""
    path = str(entry.get("path") or DEFAULT_FILE_CHANNEL_PATH)
    return FileChannel(path=path)


def _build_desktop(entry: Dict[str, Any]) -> BaseChannel:
    """构造桌面通知通道。"""
    title = str(entry.get("title") or DEFAULT_DESKTOP_TITLE)
    timeout = entry.get("timeout")
    return DesktopChannel(
        title=title,
        timeout=float(timeout) if timeout else DEFAULT_TIMEOUT,
    )


def _build_sound(entry: Dict[str, Any]) -> BaseChannel:
    """构造声音通道。"""
    timeout = entry.get("timeout")
    return SoundChannel(
        timeout=float(timeout) if timeout else DEFAULT_TIMEOUT,
    )


def _build_webhook(entry: Dict[str, Any]) -> BaseChannel:
    """构造 Webhook 通道。

    配置项：

    * ``url`` —— 接收地址（必填）。
    * ``format`` —— 报文格式：``dingtalk`` / ``feishu`` / ``serverchan`` /
      ``bark`` / ``generic``。
    * ``card`` —— 是否发 Markdown 卡片（默认否，发纯文本）。
    * ``secret`` —— 钉钉 / 飞书的加签密钥。
    * ``triggers`` —— 只推这些触发类型；不配置**或留空列表**则**推全部**
      （保持 v0.x 的旧行为）。
    * ``dedup_seconds`` —— 防骚扰窗口（默认 60 秒）；``0`` 表示不限制。
    * ``sound`` —— Bark 的提示音名。
    * ``max_retries`` —— 失败后的最大重试次数（默认 2，不含首次尝试）。
    * ``retry_backoff`` —— 重试退避基数（默认 1 秒，实际等待 = 基数 × 2ⁿ）。
    """
    timeout = entry.get("timeout")
    raw_triggers = entry.get("triggers")
    triggers: Optional[List[str]] = None
    if isinstance(raw_triggers, str):
        triggers = [raw_triggers.strip()]
    elif isinstance(raw_triggers, (list, tuple)):
        triggers = [str(item).strip() for item in raw_triggers if str(item).strip()]
    # 列表为空 = 「留空 = 推全部」，必须归一成 None（不过滤）。
    # 否则用户清空触发类型后，所有告警会被静默丢掉 —— 深夜出事却没人收到通知。
    if triggers is not None and not triggers:
        triggers = None
    dedup = entry.get("dedup_seconds")
    retries = entry.get("max_retries")
    backoff = entry.get("retry_backoff")
    return WebhookChannel(
        url=str(entry.get("url") or ""),
        timeout=float(timeout) if timeout else DEFAULT_TIMEOUT,
        format=str(entry.get("format") or "generic"),
        triggers=triggers,
        dedup_seconds=float(dedup) if dedup is not None else DEFAULT_DEDUP_SECONDS,
        secret=str(entry.get("secret") or ""),
        card=bool(entry.get("card", False)),
        sound=str(entry.get("sound") or ""),
        max_retries=int(retries) if retries is not None else DEFAULT_MAX_RETRIES,
        retry_backoff=(
            float(backoff) if backoff is not None else DEFAULT_RETRY_BACKOFF
        ),
    )


#: 通道类型 → 构造函数。类型不在表里的配置项会被跳过并记 warning。
_CHANNEL_BUILDERS: Dict[str, Callable[[Dict[str, Any]], BaseChannel]] = {
    "console": _build_console,
    "file": _build_file,
    "desktop": _build_desktop,
    "sound": _build_sound,
    "webhook": _build_webhook,
}


class Notifier:
    """告警分发器。

    :param config: 已加载的配置字典；为 ``None`` 时自行从配置文件读取。
    :param config_path: 配置文件路径（``config`` 为 ``None`` 时生效）。

    构造完成后 :attr:`channels` 就是最终生效的通道列表，可以直接
    断言，也可以由调用方替换（例如测试里注入假通道）。
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        config_path: Optional[str] = None,
    ) -> None:
        if config is None:
            config = load_config(config_path)
        self.config: Dict[str, Any] = config or {}
        raw_alert = self.config.get("alert")
        self.alert_config: Dict[str, Any] = (
            raw_alert if isinstance(raw_alert, dict) else {}
        )
        #: 告警总开关。
        self.enabled = bool(self.alert_config.get("enabled", True))
        #: 最低投递等级（低于此等级的告警被丢弃）。
        self.min_level = str(
            self.alert_config.get("min_level") or DEFAULT_MIN_LEVEL
        ).strip().lower()
        #: 已启用的通道（构造失败或类型未知的已被跳过）。
        self.channels: List[BaseChannel] = self._build_channels()
        #: 合并窗口（秒）；配置 ``alert.merge_seconds`` 覆盖。
        merge_window = self.alert_config.get("merge_seconds")
        self.merge_seconds = (
            float(merge_window)
            if merge_window is not None
            else DEFAULT_MERGE_SECONDS
        )
        #: 告警合并器 —— 防止重复告警反复敲门。
        self.coalescer = AlertCoalescer(window_seconds=self.merge_seconds)
        #: 合并摘要看门狗（后台线程）。
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        if self.merge_seconds > 0:
            self._start_watchdog()

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    def _build_channels(self) -> List[BaseChannel]:
        """按配置装配通道列表。

        单个通道初始化失败（路径不可写、参数非法等）只跳过该通道，
        不影响其余通道 —— 一个坏通道不该让整套通知失效。
        """
        raw_channels = self.alert_config.get("channels")
        if not isinstance(raw_channels, list):
            return []

        channels: List[BaseChannel] = []
        for entry in raw_channels:
            if not isinstance(entry, dict):
                logger.warning("告警通道配置不是字典，已跳过：%r", entry)
                continue
            if not bool(entry.get("enabled", True)):
                continue
            raw_type = entry.get("type")
            channel_type = str(raw_type or "").strip().lower()
            builder = _CHANNEL_BUILDERS.get(channel_type)
            if builder is None:
                logger.warning(
                    "未知的告警通道类型「%s」，已跳过。", raw_type
                )
                continue
            try:
                channel = builder(entry)
            except Exception as exc:  # noqa: BLE001 —— 配置错误不致命
                logger.warning(
                    "告警通道「%s」初始化失败，已跳过：%s: %s",
                    channel_type,
                    type(exc).__name__,
                    exc,
                )
                continue
            channels.append(channel)
        return channels

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def notify(self, alert: Dict[str, Any]) -> None:
        """把一条告警分发给所有已启用通道。

        :param alert: 告警字典（来自 ``SentinelEngine._build_alert``）。

        **本方法绝不抛异常**：任何一个通道抛错都只会被隔离成一条
        warning，其余通道照常收到这条告警。
        """
        if not self.enabled:
            return

        data = alert if isinstance(alert, dict) else {}
        level = alert_level(data)
        if level_rank(level) < level_rank(self.min_level):
            logger.debug(
                "告警等级 %s 低于阈值 %s，已过滤。", level, self.min_level
            )
            return

        for channel in self.channels:
            name = getattr(channel, "name", type(channel).__name__)
            payload = data
            # --- 合并窗口（只作用于通知型通道）---
            #
            # 记录型通道（终端 / 文件）是**流水账**，一条都不能少；
            # 通知型通道（弹窗 / 声音 / Webhook）是**敲门**，
            # 同一条告警连续敲 5 次会把用户逼疯。
            if getattr(channel, "is_notification", False):
                decision, merged = self.coalescer.decide(data)
                if decision == "merge":
                    logger.info(
                        "告警合并：%s 秒内的同类告警已并入上一条，"
                        "本次不重复通知。",
                        int(self.merge_seconds),
                    )
                    continue
                if merged:
                    payload = dict(data)
                    payload["merged_count"] = merged
            try:
                delivered = channel.send(payload)
            except Exception as exc:  # noqa: BLE001 —— 通道失败绝不外泄
                logger.warning(
                    "告警通道「%s」投递异常，已隔离：%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
            if not delivered:
                logger.warning("告警通道「%s」投递失败。", name)

    def push_briefing(
        self, summary: Dict[str, Any], *, timeout: float = 10.0
    ) -> bool:
        """把**晨报摘要**推给通知型通道。

        和普通告警分开走 —— 晨报是「一夜的总结」，
        不是「刚发生的坏事」：

        * **只推通知型通道**（Webhook）——
          终端 / 文件通道是流水账，晨报已经落盘了，不必再写一遍；
        * **不过告警的等级过滤与合并窗口** ——
          晨报是**一次性**投递，套上那些规则只会被误伤
          （比如用户配了 ``triggers: [DANGEROUS]``，
          晨报就会被当成「不是 DANGEROUS」而永远推不出去）。

        ⚠️ **会等投递完成**（v3.2.0）
        ==============================
        投递是**后台线程**做的（不能卡住主循环）。
        但晨报是**收尾时**才推的 —— 那时用户正要关进程，
        等一下返回「已推送」很可能只是「已入队」。

        所以这里等队列跑完（最多 ``timeout`` 秒），
        再根据**真实投递结果**返回成功与否。
        宁可让收尾多等几秒，也不要对用户说谎。

        **本方法绝不抛异常** —— 晨报推不出去不该影响收尾。

        :param summary: 晨报摘要（见
            :func:`src.core.briefing.build_briefing_summary`）。
        :param timeout: 等投递完成的最长秒数。
        :return: 至少一个通道**真的投递成功**返回 ``True``。
        """
        data = summary if isinstance(summary, dict) else {}
        queued = False
        # ⚠️ 推送前先记下各通道的**送达计数**（v3.2.0 修的真缺陷）。
        #
        # 之前是看 ``channel.last_delivered`` —— 那是「最近一次」的结果，
        # 可能是一条**更早的告警**留下的。
        # 于是「晨报根本没送到」也会返回 ``True``，
        # 终端印一句「晨报摘要已推送到你的手机」，用户信了，
        # 早上却什么都没收到 —— 这比不推还糟。
        baselines: Dict[int, int] = {}
        for channel in self.channels:
            sender = getattr(channel, "send_briefing", None)
            if sender is None:
                continue
            count = getattr(channel, "delivered_count", None)
            if isinstance(count, int):
                baselines[id(channel)] = count
            name = getattr(channel, "name", type(channel).__name__)
            try:
                if sender(data):
                    queued = True
                else:
                    logger.warning(
                        "晨报未能投递到通道「%s」（地址为空 / 队列满 / 协议不允许）。",
                        name,
                    )
            except Exception as exc:  # noqa: BLE001 —— 通道失败绝不外泄
                logger.warning(
                    "晨报投递到通道「%s」时异常，已隔离：%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
        if not queued:
            return False

        # 等队列跑完 —— 不然「已推送」只是一句空话。
        delivered = False
        for channel in self.channels:
            flusher = getattr(channel, "flush", None)
            if flusher is None:
                continue
            name = getattr(channel, "name", type(channel).__name__)
            try:
                flusher(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 —— 等不到不影响结论
                logger.warning(
                    "等待通道「%s」投递完成时异常：%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
            baseline = baselines.get(id(channel))
            current = getattr(channel, "delivered_count", None)
            if isinstance(baseline, int) and isinstance(current, int):
                # 计数涨了 → **这一条**真的送出去了。
                if current > baseline:
                    delivered = True
            elif getattr(channel, "last_delivered", False):
                delivered = True
        if delivered:
            logger.info("晨报摘要已投递给通知型通道。")
        else:
            failures = sum(
                int(getattr(channel, "failed_sends", 0) or 0)
                for channel in self.channels
            )
            logger.warning(
                "晨报摘要未能投递到任何通知型通道（累计失败 %d 次）。",
                failures,
            )
        return delivered

    def flush(self, timeout: float = 10.0) -> bool:
        """等所有通道把**已入队**的报文投完。

        :param timeout: 最长等待秒数。
        :return: 全部投完返回 ``True``；有通道没投完返回 ``False``。
        """
        ok = True
        for channel in self.channels:
            flusher = getattr(channel, "flush", None)
            if flusher is None:
                continue
            try:
                if not flusher(timeout=max(float(timeout), 0.0)):
                    ok = False
            except Exception as exc:  # noqa: BLE001 —— 等不到不影响结论
                logger.warning(
                    "等待通道「%s」投递完成时异常：%s: %s",
                    getattr(channel, "name", type(channel).__name__),
                    type(exc).__name__,
                    exc,
                )
                ok = False
        return ok

    def close(self) -> None:
        """关闭所有通道，释放文件句柄等资源。"""
        self._watchdog_stop.set()
        for channel in self.channels:
            name = getattr(channel, "name", type(channel).__name__)
            try:
                channel.close()
            except Exception as exc:  # noqa: BLE001 —— 清理失败不外泄
                logger.warning(
                    "关闭告警通道「%s」失败：%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        """起一个后台线程，把**过期**的合并摘要补推出去。"""
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name="sentinel-merge-watchdog",
            daemon=True,
        )
        self._watchdog.start()

    def _watchdog_loop(self) -> None:
        """看门狗主体 —— 定期检查有没有攒下没告诉用户的合并条数。"""
        interval = max(min(self.merge_seconds / 4.0, 5.0), 0.5)
        while not self._watchdog_stop.wait(interval):
            try:
                summaries = self.coalescer.expired_summaries()
            except Exception as exc:  # noqa: BLE001 —— 看门狗失败不影响审查
                logger.warning(
                    "合并摘要检查失败：%s: %s", type(exc).__name__, exc
                )
                continue
            for summary in summaries:
                self._push_summary(summary)

    def _push_summary(self, alert: Dict[str, Any]) -> None:
        """把一条合并摘要推给通知型通道（**不**再走合并判断）。"""
        for channel in self.channels:
            if not getattr(channel, "is_notification", False):
                continue
            name = getattr(channel, "name", type(channel).__name__)
            try:
                channel.send(alert)
            except Exception as exc:  # noqa: BLE001 —— 通道失败不外泄
                logger.warning(
                    "合并摘要投递异常（%s）：%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )

    def format(self, alert: Dict[str, Any]) -> str:
        """把告警渲染成人话（与各通道使用的格式一致）。"""
        return format_alert(alert)

    def __enter__(self) -> "Notifier":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
