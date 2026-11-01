"""一键复活 —— 回滚 + 注入纠偏提示 + 重新唤醒 Agent。

v2.0.0 给了哨兵三块拼图：安全点快照、留言箱注入、守护者挂起。
但用户要**手动**把它们串起来：点回滚、点发送、再去终端重启进程。
半夜挂 10 个任务的时候，这三步能把人逼疯。

这一版把它们合并成**一次点击**。

为什么要「终止 + 重拉」而不是「放行」
========================================

被哨兵挂起的进程，正卡在一条**危险命令**上 ——
挂起就是为了让它来不及执行那条命令。

所以「放行它」**不是复活，那是闯祸**：
刚被拦下的 ``rm -rf`` 一放行就跑了。

于是复活只有一条安全的路：

1. **回滚** —— 把工作目录退回生效的安全点（改坏的文件恢复、新建的删掉）；
2. **注入** —— 把拦截原因和纠偏提示写进留言箱；
3. **结束** —— 结束那个卡在危险命令上的进程
   （红线 R6 的唯一例外，作用域见
   :meth:`src.core.guardian.Guardian.terminate_suspended`）；
4. **重拉** —— 重新拉起 Agent，这次任务提示里**带着**那条纠偏提示。

所以顺序不能乱：**先注入，再重拉** ——
重拉时任务提示才会把纠偏提示带上。

模块边界
========

这一层**不认识** Agent 进程。它只认识一个「鸭子类型」
:class:`AgentControl`：

* ``state()``      → 当前进程状态（pid / 是否挂起 / 是否还活着）；
* ``terminate()``  → 结束挂起的进程；
* ``relaunch()``   → 重新拉起一个带纠偏提示的 Agent。

真正的进程操作由**启动器**实现（它手上有 Popen），
看板只负责调 :meth:`ResurrectService.execute` ——
这条边界和 v2.0.0 的 ``inject_callback`` / ``rollback_callback`` 完全一致：
**GUI 应用层不认识引擎**。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


#: 复活成功后固定播报的那句话。
#:
#: 终端与看板都打印这一句 —— 两边看到的是**同一句**，
#: 免得用户对着两套说法猜是不是同一件事。
RESURRECT_MESSAGE = (
    "[SENTINEL] 已回滚至安全点，并注入纠偏提示，重新唤醒 Agent。"
)


class ResurrectService:
    """把「回滚 / 注入 / 重拉」三步编排成一次操作。

    每个会话应当有**自己的一实例** ——
    这样「多个会话同时挂起」时，它们各回各的，互不影响
    （并发防呆靠的就是这一层：每个实例一把自己的锁）。

    :param safe_point_service: :class:`src.core.safepoint.SafePointService`。
    :param injector: :class:`src.core.injector.AgentInjector`（写留言箱 / stdin）。
    :param guardian: 守护者（用来查挂起状态、结束挂起进程）。
    :param agent_control: 进程控制（鸭子类型，见模块文档）。
    :param label: 会话名（写进播报与日志）。
    """

    def __init__(
        self,
        *,
        safe_point_service: Any = None,
        injector: Any = None,
        guardian: Any = None,
        agent_control: Any = None,
        label: str = "",
    ) -> None:
        self.safe_point_service = safe_point_service
        self.injector = injector
        self.guardian = guardian
        self.agent_control = agent_control
        self.label = str(label or "")
        # 每个实例一把锁 —— 同一会话的并发点击只有一个能进去。
        self._lock = threading.Lock()
        self._busy = False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def agent_state(self) -> Dict[str, Any]:
        """当前 Agent 进程的状态（拿不到就如实说拿不到）。

        :return: ``{"ok": bool, "pid": int, "suspended": bool, ...}``。
        """
        if self.agent_control is None:
            return {"ok": False, "detail": "这个会话没有接入 Agent 进程控制。"}
        try:
            state = self.agent_control.state()
        except Exception as exc:  # noqa: BLE001 —— 查询失败只降级
            logger.warning(
                "查 Agent 状态失败：%s: %s", type(exc).__name__, exc
            )
            return {"ok": False, "detail": f"查 Agent 状态失败：{exc}"}
        result = dict(state or {})
        result.setdefault("ok", True)
        return result

    def plan(self, point_id: str = "", workdir: str = "") -> Dict[str, Any]:
        """算出复活**将要**做什么（只算不动）。

        看板先把这个计划给用户看，用户点了确认才执行 ——
        权限小于用户。

        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录。
        :return: 计划字典。
        """
        result: Dict[str, Any] = {
            "ok": False,
            "label": self.label,
            "restored": [],
            "deleted": [],
            "restored_count": 0,
            "deleted_count": 0,
            "agent": self.agent_state(),
            "steps": [],
        }
        if self.safe_point_service is None:
            result["error"] = "这个会话没有配置安全点服务，没法回滚。"
            return result

        try:
            plan = self.safe_point_service.plan(point_id, workdir)
        except Exception as exc:  # noqa: BLE001 —— 计划失败只降级
            logger.warning("算回滚计划失败：%s: %s", type(exc).__name__, exc)
            result["error"] = f"算回滚计划失败：{exc}"
            return result

        result["ok"] = bool(plan.get("ok"))
        result["point"] = plan.get("point")
        result["workdir"] = plan.get("workdir")
        result["restored"] = plan.get("restored") or []
        result["deleted"] = plan.get("deleted") or []
        result["restored_count"] = plan.get("restored_count") or 0
        result["deleted_count"] = plan.get("deleted_count") or 0
        if not result["ok"]:
            result["error"] = plan.get("error") or "没有可回滚的安全点。"

        steps: List[str] = []
        if result["restored_count"] or result["deleted_count"]:
            steps.append(
                "把工作目录回滚到安全点"
                f"（恢复 {result['restored_count']} 个、"
                f"删除 {result['deleted_count']} 个）"
            )
        else:
            steps.append("工作目录已经是干净的了，不用回滚")
        steps.append("把拦截原因和纠偏提示写进留言箱")
        if result["agent"].get("suspended"):
            steps.append("结束那个卡在危险命令上的挂起进程")
            steps.append("重新拉起 Agent（任务提示里带上纠偏提示）")
        else:
            steps.append("Agent 没有处于挂起状态，只需重新拉起")
        result["steps"] = steps
        return result

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(
        self,
        prompt: str,
        meta: Optional[Dict[str, Any]] = None,
        *,
        point_id: str = "",
        workdir: str = "",
    ) -> Dict[str, Any]:
        """执行一次复活：回滚 → 注入 → 结束挂起进程 → 重拉 Agent。

        **绝不抛异常** —— 审查侧的异常一律降级（架构红线 R5）。

        :param prompt: 纠偏提示正文（通常是「拦截原因 + 纠偏建议」）。
        :param meta: 附加信息（告警 ID、规则 ID 等），只用于留痕。
        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录。
        :return: 结果字典（含 ``message`` 播报文案）。
        """
        info = dict(meta or {})
        if self._busy:
            return {
                "ok": False,
                "error": "上一个复活操作还在进行中，请等它结束。",
                "busy": True,
            }
        self._busy = True
        started = time.monotonic()
        try:
            return self._execute_locked(prompt, info, point_id, workdir, started)
        except Exception as exc:  # noqa: BLE001 —— 任何异常都降级
            logger.warning("复活失败（已降级）：%s: %s", type(exc).__name__, exc)
            return {
                "ok": False,
                "error": f"复活失败：{type(exc).__name__}: {exc}",
            }
        finally:
            self._busy = False

    def _execute_locked(
        self,
        prompt: str,
        info: Dict[str, Any],
        point_id: str,
        workdir: str,
        started: float,
    ) -> Dict[str, Any]:
        """真正干活（调用方已拿到锁）。"""
        result: Dict[str, Any] = {
            "ok": False,
            "label": self.label,
            "steps": [],
            "meta": info,
        }

        # ---- 1. 回滚 --------------------------------------------------
        restore: Dict[str, Any] = {}
        if self.safe_point_service is not None:
            try:
                restore = self.safe_point_service.restore(point_id, workdir)
            except Exception as exc:  # noqa: BLE001 —— 回滚失败只降级
                logger.warning("回滚失败：%s: %s", type(exc).__name__, exc)
                restore = {"ok": False, "error": f"回滚失败：{exc}"}
        result["restore"] = restore
        result["restored_count"] = restore.get("restored_count") or 0
        result["deleted_count"] = restore.get("deleted_count") or 0
        result["restore_elapsed"] = restore.get("elapsed")
        result["steps"].append(
            "已回滚到安全点"
            f"（恢复 {result['restored_count']} 个、"
            f"删除 {result['deleted_count']} 个）"
        )

        # ---- 2. 注入 --------------------------------------------------
        injection: Dict[str, Any] = {}
        if self.injector is not None:
            try:
                injection = self.injector.inject(
                    prompt,
                    session_id=str(info.get("session_id") or ""),
                    verdict=str(info.get("verdict") or ""),
                    rule_id=str(info.get("rule_id") or ""),
                    reason=str(info.get("reason") or ""),
                    extra=dict(info.get("extra") or {}),
                )
            except Exception as exc:  # noqa: BLE001 —— 注入失败只降级
                logger.warning("注入失败：%s: %s", type(exc).__name__, exc)
                injection = {"ok": False, "detail": f"注入失败：{exc}"}
        if hasattr(injection, "to_dict"):
            injection = injection.to_dict()
        result["injection"] = injection
        result["inject_channel"] = injection.get("channel")
        result["steps"].append(
            "已注入纠偏提示"
            f"（通道：{injection.get('channel') or '未知'}）"
        )

        # ---- 3. 结束挂起进程 ------------------------------------------
        terminated = False
        if self.guardian is not None:
            try:
                outcome = self.guardian.terminate_suspended()
                terminated = bool(outcome.get("ok"))
                result["terminate"] = outcome
                result["steps"].append(
                    "已结束挂起的进程" if terminated else "无需结束挂起的进程"
                )
            except Exception as exc:  # noqa: BLE001 —— 结束失败只降级
                logger.warning("结束挂起进程失败：%s: %s", type(exc).__name__, exc)
                result["terminate"] = {"ok": False, "error": str(exc)}
                result["steps"].append("结束挂起进程失败（已跳过）")

        # ---- 4. 重新拉起 ----------------------------------------------
        relaunch: Dict[str, Any] = {
            "ok": False,
            "detail": "没有进程控制能力，无法重新拉起 Agent。",
        }
        if self.agent_control is not None:
            try:
                relaunch = self.agent_control.relaunch(prompt)
            except Exception as exc:  # noqa: BLE001 —— 重拉失败只降级
                logger.warning("重拉 Agent 失败：%s: %s", type(exc).__name__, exc)
                relaunch = {"ok": False, "detail": f"重拉失败：{exc}"}
        result["relaunch"] = relaunch
        result["new_pid"] = relaunch.get("pid")
        result["steps"].append(
            "已重新拉起 Agent"
            if relaunch.get("ok")
            else "重新拉起 Agent 失败"
        )

        result["elapsed"] = round(time.monotonic() - started, 3)
        result["ok"] = bool(relaunch.get("ok"))
        result["message"] = RESURRECT_MESSAGE
        if not result["ok"]:
            result["error"] = (
                str(relaunch.get("detail") or "") or "重新拉起 Agent 失败"
            )
        return result
