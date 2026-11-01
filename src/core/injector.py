"""指令注入中枢 —— 哨兵怎么把一句话送到 Agent 那里。

v1.2.0 的哨兵只能**看**和**复制**：告警卡片上有个【复制纠偏提示】，
用户得自己切到 Agent 的窗口去粘贴。半夜挂机时这很别扭 ——
人不在电脑前，Agent 却卡在死循环里。

v2.0.0 给哨兵加上了「说一句话」的能力。难点不在于写文件，
而在于**哨兵和 Agent 往往不是同一个进程**：

* 看板（``src.gui.server``）是一个进程；
* 启动器（``run_with_sentinel.py``）拉起 Agent，是**另一个**进程；
* Agent 又是第三个进程。

所以「把提示写进 Agent 的 stdin」这件事，**看板自己做不到** ——
它连 Agent 的管道句柄都没有。

本模块给出的答案是**留言箱（inbox）**：

====================== =================================================
环节                  谁来做
====================== =================================================
看板写留言            :class:`InboxStore` 追加一条 JSON
启动器读留言          轮询留言箱，发现新消息就**转写进 Agent 的 stdin**
Agent 收指令          stdin 管道（能收就收）
收不到怎么办          消息留在留言箱里，下次启动时**注入任务提示**
====================== =================================================

这样「看板 → Agent」就变成了一条**跨进程、可追溯、不会丢**的链路，
而且**降级路径和成功路径是同一条**：
写进留言箱 → 被消费 → 标记已送达。

两条硬约束：

* **审查侧异常绝不外泄** —— 注入失败只降级到留言箱，绝不抛异常；
* **不替用户做决定** —— 哨兵只是「把话说过去」，
  采不采纳、怎么改，全是 Agent 和用户的事。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "DEFAULT_INBOX_NAME",
    "INBOX_FILENAME",
    "AgentInjector",
    "InboxStore",
    "InjectionResult",
]

logger = logging.getLogger(__name__)

#: 留言箱文件名（放在被监控 Agent 的工作目录里）。
INBOX_FILENAME = "sentinel_inbox.json"

#: 留言箱默认文件名（兼容别名）。
DEFAULT_INBOX_NAME = INBOX_FILENAME

#: 留言箱的格式版本。
INBOX_VERSION = 1


@dataclass
class InjectionResult:
    """一次注入的结果。

    看板据此告诉用户「话送到哪儿了」以及「你接下来该做什么」。

    :param ok: 是否**成功送达**（写进 stdin 或写进留言箱都算送达）。
    :param channel: 送达渠道 —— ``stdin`` / ``inbox``。
    :param detail: 一句话说明（中文）。
    :param next_step: 给用户看的下一步指引（中文）；没有就空。
    :param inbox_path: 留言箱路径。
    :param message_id: 留言 ID。
    :param prompt: 实际送出去的提示正文。
    """

    ok: bool = False
    channel: str = ""
    detail: str = ""
    next_step: str = ""
    inbox_path: str = ""
    message_id: str = ""
    prompt: str = ""
    merged_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（给 API / JSON 用）。"""
        return {
            "ok": bool(self.ok),
            "channel": str(self.channel),
            "detail": str(self.detail),
            "next_step": str(self.next_step),
            "inbox_path": str(self.inbox_path),
            "message_id": str(self.message_id),
            "prompt": str(self.prompt),
            "merged_count": int(self.merged_count),
        }


@dataclass
class InboxMessage:
    """留言箱里的一条消息。"""

    id: str = ""
    created_at: float = 0.0
    session_id: str = ""
    agent: str = ""
    verdict: str = ""
    rule_id: str = ""
    reason: str = ""
    prompt: str = ""
    delivered: bool = False
    delivered_at: float = 0.0
    channel: str = "inbox"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典。"""
        data = {
            "id": self.id,
            "created_at": float(self.created_at),
            "session_id": self.session_id,
            "agent": self.agent,
            "verdict": self.verdict,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "prompt": self.prompt,
            "delivered": bool(self.delivered),
            "delivered_at": float(self.delivered_at),
            "channel": self.channel,
        }
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InboxMessage":
        """从字典还原（文件里读出来的）。"""
        raw = dict(data or {})
        extra = raw.pop("extra", None)
        known = {
            "id",
            "created_at",
            "session_id",
            "agent",
            "verdict",
            "rule_id",
            "reason",
            "prompt",
            "delivered",
            "delivered_at",
            "channel",
        }
        rest = {k: v for k, v in raw.items() if k not in known}
        return cls(
            id=str(raw.get("id") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            session_id=str(raw.get("session_id") or ""),
            agent=str(raw.get("agent") or ""),
            verdict=str(raw.get("verdict") or ""),
            rule_id=str(raw.get("rule_id") or ""),
            reason=str(raw.get("reason") or ""),
            prompt=str(raw.get("prompt") or ""),
            delivered=bool(raw.get("delivered")),
            delivered_at=float(raw.get("delivered_at") or 0.0),
            channel=str(raw.get("channel") or "inbox"),
            extra=dict(extra) if isinstance(extra, dict) else rest,
        )


class InboxStore:
    """哨兵留言箱 —— 跨进程的一条指令通道。

    为什么是 JSON 文件而不是 socket
    ============================

    * 看板和启动器**不一定同时在跑**（看板可以单独起）；
    * 文件天然**可读** —— 用户半夜醒来能直接 ``type`` 出来看；
    * 断电不丢 —— 消息留在盘上，下次启动还能补投。

    :param path: 留言箱文件路径。
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path or ""))
        self._lock = threading.RLock()

    # -- 读 -------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        """读留言箱；读不出来就当空箱（绝不抛异常）。"""
        payload: Dict[str, Any] = {"version": INBOX_VERSION, "messages": []}
        if not self.path or not os.path.exists(self.path):
            return payload
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                messages = loaded.get("messages")
                if isinstance(messages, list):
                    payload["messages"] = [
                        m for m in messages if isinstance(m, dict)
                    ]
        except (OSError, ValueError) as exc:
            logger.warning(
                "读留言箱失败，按空箱处理：%s: %s", type(exc).__name__, exc
            )
        return payload

    def _save(self, payload: Dict[str, Any]) -> None:
        """写留言箱（原子替换，避免写一半被读到）。"""
        if not self.path:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    def messages(self) -> List[InboxMessage]:
        """全部消息（按时间先后）。"""
        with self._lock:
            payload = self._load()
        return [
            InboxMessage.from_dict(item) for item in payload.get("messages", [])
        ]

    def pending(self) -> List[InboxMessage]:
        """**还没送达**的消息 —— 启动器要补投的就是它们。"""
        return [msg for msg in self.messages() if not msg.delivered]

    # -- 写 -------------------------------------------------------------
    def append(self, message: InboxMessage) -> InboxMessage:
        """追加一条消息。

        :param message: 消息。
        :return: 追加后的消息（含生成的 ID 与时间戳）。
        """
        with self._lock:
            payload = self._load()
            messages = payload.setdefault("messages", [])
            if not message.id:
                message.id = _make_message_id(message.created_at or time.time())
            if not message.created_at:
                message.created_at = time.time()
            messages.append(message.to_dict())
            self._save(payload)
        return message

    def mark_delivered(self, message_ids: List[str]) -> int:
        """把若干条消息标记为已送达。

        :param message_ids: 消息 ID 列表。
        :return: 实际标记成功的条数。
        """
        wanted = {str(item) for item in (message_ids or []) if str(item)}
        if not wanted:
            return 0
        with self._lock:
            payload = self._load()
            changed = 0
            for item in payload.get("messages", []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "") in wanted and not item.get(
                    "delivered"
                ):
                    item["delivered"] = True
                    item["delivered_at"] = time.time()
                    changed += 1
            if changed:
                self._save(payload)
        return changed

    def render_for_prompt(self, *, limit: int = 3) -> str:
        """把待投消息渲染成一段可以塞进任务提示的文本。

        启动器在**拉起 Agent 之前**调用它 ——
        这样即使 Agent 完全不支持 stdin，
        「重启流程注入上下文」这条降级路也是通的。

        :param limit: 最多带几条（多了提示词会被撑爆）。
        :return: 渲染好的中文文本；没有待投消息时返回空串。
        """
        pending = self.pending()
        if not pending:
            return ""
        picked = pending[-max(int(limit or 1), 1):]
        blocks = [
            "【哨兵留言】在你开始之前，哨兵给你留了 "
            f"{len(picked)} 条纠正指令，请先读完再动手："
        ]
        for index, msg in enumerate(picked, start=1):
            blocks.append(f"\n--- 第 {index} 条 ---")
            blocks.append(msg.prompt.strip() or "(空)")
        return "\n".join(blocks)


class AgentInjector:
    """把一段提示送到 Agent 那里。

    :param inbox: 留言箱。
    :param stdin_writer: 往 Agent 的 stdin 写东西的回调；
        返回 ``True`` 表示写成功。为 ``None`` 时只走留言箱。
    :param agent_label: Agent 的名字（写进留言，方便用户辨认）。
    """

    def __init__(
        self,
        *,
        inbox: InboxStore,
        stdin_writer: Optional[Callable[[str], bool]] = None,
        agent_label: str = "",
    ) -> None:
        self.inbox = inbox
        self.stdin_writer = stdin_writer
        self.agent_label = str(agent_label or "")

    # -- 能力 -----------------------------------------------------------
    def capabilities(self) -> Dict[str, Any]:
        """当前有哪些送达渠道（看板用它决定按钮怎么显示）。"""
        return {
            "stdin": self.stdin_writer is not None,
            "inbox": bool(self.inbox.path),
            "inbox_path": self.inbox.path,
            "agent": self.agent_label,
        }

    # -- 主流程 ---------------------------------------------------------
    def inject(
        self,
        prompt: str,
        *,
        session_id: str = "",
        verdict: str = "",
        rule_id: str = "",
        reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> InjectionResult:
        """把提示送到 Agent。

        **绝不抛异常** —— 审查侧的异常不能影响被监控的 Agent
        （架构红线 R5）。

        :param prompt: 要送出去的提示正文。
        :param session_id: 会话 ID。
        :param verdict: 告警判定（``STUCK`` / ``LOOP`` / ``DANGEROUS``…）。
        :param rule_id: 命中的规则 ID。
        :param reason: 规则原因。
        :param extra: 附加信息（会存进留言）。
        :return: 注入结果。
        """
        text = str(prompt or "").strip()
        result = InjectionResult(prompt=text, inbox_path=self.inbox.path)
        if not text:
            result.detail = "提示内容为空，没有可发送的内容。"
            return result

        message = InboxMessage(
            created_at=time.time(),
            session_id=str(session_id or ""),
            agent=self.agent_label,
            verdict=str(verdict or ""),
            rule_id=str(rule_id or ""),
            reason=str(reason or ""),
            prompt=text,
            extra=dict(extra or {}),
        )

        # --- 渠道一：直接写 Agent 的标准输入 ---
        if self.stdin_writer is not None:
            try:
                written = bool(self.stdin_writer(text))
            except Exception as exc:  # noqa: BLE001 —— 写失败只降级
                logger.warning(
                    "写 Agent 标准输入失败，降级到留言箱：%s: %s",
                    type(exc).__name__,
                    exc,
                )
                written = False
            if written:
                message.channel = "stdin"
                message.delivered = True
                message.delivered_at = time.time()
                try:
                    self.inbox.append(message)
                except Exception:  # noqa: BLE001 —— 留痕失败不影响送达
                    logger.warning("写入留言箱留痕失败（指令已送达 stdin）。")
                result.ok = True
                result.channel = "stdin"
                result.message_id = message.id
                result.detail = (
                    "已写入 Agent 的标准输入 —— "
                    "该 CLI 是否消费这行内容，取决于它是否支持流式输入。"
                )
                result.next_step = (
                    "如果 Agent 界面上没有反应，说明这个 CLI 不读标准输入；"
                    "指令已留底在留言箱里，可随时手动粘贴。"
                )
                return result

        # --- 渠道二：写进留言箱（跨进程、可追溯） ---
        try:
            saved = self.inbox.append(message)
        except Exception as exc:  # noqa: BLE001 —— 连留言都写不了才算失败
            logger.warning(
                "写留言箱失败：%s: %s", type(exc).__name__, exc
            )
            result.detail = (
                f"指令没能送出去（写留言箱失败：{type(exc).__name__}）。"
                "请手动把上面的提示粘给 Agent。"
            )
            return result

        result.ok = True
        result.channel = "inbox"
        result.message_id = saved.id
        result.detail = (
            "Agent 当前不接受标准输入，指令已写入留言箱："
            f"{self.inbox.path}"
        )
        result.next_step = _inbox_next_step(self.inbox.path)
        return result


def _inbox_next_step(path: str) -> str:
    """生成给用户看的「下一步该做什么」。

    :param path: 留言箱路径。
    :return: 中文指引。
    """
    return (
        "接下来你可以：\n"
        "  1) 重新跑一次哨兵 —— 启动器会自动把这条指令注入 Agent 的任务提示；\n"
        "  2) 直接把上面的提示粘进 Agent 的对话窗口；\n"
        f"  3) 随时查看留言箱：{path}"
    )


def _make_message_id(stamp: float) -> str:
    """生成一条留言的 ID。

    :param stamp: 时间戳。
    :return: 形如 ``20261027T174512-123456`` 的 ID。
    """
    return "{}-{:06d}".format(
        time.strftime("%Y%m%dT%H%M%S", time.localtime(stamp)),
        int((stamp % 1.0) * 1_000_000),
    )
