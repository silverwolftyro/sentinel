"""一路会话的后台跟读线程（v5.0.0）。

它做什么
========

* 跟读**这一路自己的**日志文件（``FileTailer``）；
* 用**这一路自己的**适配器把原始行解析成统一事件；
* 用**这一路自己的** L1 规则引擎判定（``LoopDetector`` 有状态，
  共享就等于把 A 路的「读同一文件三次」算到 B 路头上）；
* 命中红线 → **终端打印** + （配了回调地址就）**POST 一条极简报警**。

⚠️ **不弹窗** —— v5.0.0 起哨兵是**无头守护进程**，
没有任何 UI 呈现层，报警只走「终端 + 外部回调」两条路。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.adapters.base import BaseAdapter
from src.adapters.claude import ClaudeAdapter
from src.adapters.codex import CodexAdapter
from src.adapters.mcode import McodeAdapter
from src.adapters.text import TextAdapter
from src.rules.basic import VERDICT_DANGEROUS, RuleEngine
from src.tailer import FileTailer

from .context import SessionContext

logger = logging.getLogger(__name__)

__all__ = [
    "SessionWorker",
    "adapter_for",
]

#: Agent 名 → 适配器类。认不出来就用纯文本适配器（宁可少判，不可乱判）。
_ADAPTERS: Dict[str, Any] = {
    "mcode": McodeAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}


def adapter_for(agent: str) -> BaseAdapter:
    """按 Agent 名挑适配器。

    :param agent: Agent 类型（小写）。
    :return: 适配器实例；认不出来就是 :class:`TextAdapter`。
    """
    key = str(agent or "").strip().lower()
    factory = _ADAPTERS.get(key)
    if factory is None:
        return TextAdapter()
    return factory()


class SessionWorker(threading.Thread):
    """一路会话的后台跟读线程。"""

    def __init__(
        self,
        context: SessionContext,
        adapter: BaseAdapter,
        *,
        poll_interval: float = 0.5,
        on_alert: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """建线程（**还没启动**）。

        :param context: 这一路的会话上下文。
        :param adapter: 这一路的适配器。
        :param poll_interval: 跟读轮询间隔（秒）。
        :param on_alert: 命中红线时的回调（可为 ``None``）。
        """
        super().__init__(daemon=True, name="sentinel-session")
        self.context = context
        self.adapter = adapter
        self.poll_interval = max(float(poll_interval), 0.05)
        self.on_alert = on_alert
        self._stop = threading.Event()
        self._tailer: Optional[FileTailer] = None

    # -- 生命周期 --------------------------------------------------------
    def stop(self) -> None:
        """让线程在下一轮轮询后退出。"""
        self._stop.set()

    def run(self) -> None:
        """主循环：跟读 → 解析 → 判定 → 报警。"""
        log_path = str(self.context.log_path or "")
        if not log_path:
            return
        try:
            self._tailer = FileTailer(log_path)
            # 只跟**新写**的内容 —— 历史不该被当成「刚刚发生」。
            self._tailer.skip_to_end()
        except Exception as exc:  # noqa: BLE001 —— 跟读失败不能拖垮守护
            logger.warning(
                "会话 %s 跟读启动失败：%s: %s",
                self.context.fingerprint[:40],
                type(exc).__name__,
                exc,
            )
            return

        while not self._stop.is_set():
            try:
                lines = self._tailer.poll()
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不致命
                logger.debug("会话跟读一轮失败：%s: %s", type(exc).__name__, exc)
                lines = []
            for line in lines:
                self._handle_line(line)
            self._stop.wait(self.poll_interval)

    # -- 处理 ------------------------------------------------------------
    def _handle_line(self, line: str) -> None:
        """处理一行原始文本。"""
        text = str(line or "").strip()
        if not text:
            return
        try:
            events = self.adapter.parse_line(text)
        except Exception as exc:  # noqa: BLE001 —— 坏行跳过
            logger.debug("解析行失败：%s: %s", type(exc).__name__, exc)
            return
        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: Dict[str, Any]) -> None:
        """处理一个统一事件。"""
        context = self.context
        context.events += 1
        context.updated_at = time.time()
        rules = context.rules
        if rules is None:
            return
        try:
            verdict = rules.check_event(event)
        except Exception as exc:  # noqa: BLE001 —— 判定失败不致命
            logger.debug("L1 判定失败：%s: %s", type(exc).__name__, exc)
            return
        name = str(verdict.get("verdict") or "")
        context.last_verdict = name
        context.last_reason = str(verdict.get("reason") or "")
        if name != VERDICT_DANGEROUS:
            return
        context.alerts += 1
        alert = {
            "action": "block",
            "verdict": name,
            "rule_id": str(verdict.get("rule_id") or ""),
            "reason": context.last_reason,
            "evidence": str(verdict.get("evidence") or ""),
            "session_id": context.session_id,
            "cwd": context.cwd,
            "pid": context.pid,
            "agent": context.agent,
            "goal": context.goal,
            "ts": time.time(),
        }
        logger.warning(
            "[红线] %s | %s | %s",
            context.agent or context.session_id or "?",
            alert["rule_id"] or "危险操作",
            context.last_reason,
        )
        if self.on_alert is not None:
            try:
                self.on_alert(alert)
            except Exception as exc:  # noqa: BLE001 —— 报警失败不影响审查
                logger.warning(
                    "报警回调失败（已忽略）：%s: %s", type(exc).__name__, exc
                )


def build_session_rules(
    config: Optional[Dict[str, Any]] = None,
    config_path: Optional[str] = None,
) -> RuleEngine:
    """给一路会话造**它自己**的规则引擎。

    ⚠️ 必须是**每一路一份** ——
    :class:`~src.rules.basic.LoopDetector` 是有状态的
    （记着「这个文件读了几次」），共享就串台。

    :param config: 已加载的配置字典。
    :param config_path: 配置文件路径。
    :return: 规则引擎实例。
    """
    return RuleEngine(config, config_path=config_path)


def collect_lines(lines: List[str]) -> List[str]:
    """过滤掉空行。

    :param lines: 原始行。
    :return: 非空行。
    """
    return [line for line in lines if str(line or "").strip()]
