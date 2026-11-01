"""守护者（Guardian）—— 哨兵唯一的「主动干预」能力。

v0.5.0 之前哨兵是彻底的旁观者：看到 ``rm -rf /`` 也只是弹个窗。
守护者模式给了哨兵**极小的一点点**干预权，但这一点点必须同时满足
四条硬约束：

============ ==================================================
约束          含义
============ ==================================================
可逆          只挂起 / 只发中断信号，任何时候都能恢复
小于用户      用户随时可以否决（override），否决立即生效
有留痕        每次干预与放行都写进 ``logs/``
不伤及无辜    绝不 kill；不碰 PID 0 / 1 / 自身 / 父进程
============ ==================================================

平台差异（很重要）
==================

**Windows 上不能用 ``os.kill(pid, signal.SIGINT)``** ——
CPython 在 Windows 上会把它翻译成 ``TerminateProcess``，
那不是「中断」而是「终止」，被监控的 Agent 会直接消失。
所以 Windows 上的默认动作是**挂起**
（``psutil.Process(pid).suspend()``），POSIX 上才是 SIGINT
（真正的 Ctrl+C 语义）。

进程控制被单独抽成 :class:`ProcessController`，
这样测试可以 Mock 掉信号发送，而不必真的去动任何进程 ——
**测试永远不该真的挂起谁的进程**。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

__all__ = [
    "INTERCEPT_MESSAGE",
    "Guardian",
    "ProcessController",
    "load_guardian",
]

logger = logging.getLogger(__name__)

#: 拦截发生时的固定提示文案。
#:
#: 任务书要求的原文是「[哨兵已拦截] 发现高危操作，已暂停目标进程。
#: 输入 'override' 可放行并让哨兵在当前任务中不再拦截该规则」。
#: 这里保持一字不差 —— 用户要能一眼认出这句话代表「我还有否决权」。
INTERCEPT_MESSAGE = (
    "[哨兵已拦截] 发现高危操作，已暂停目标进程。"
    "输入 'override' 可放行并让哨兵在当前任务中不再拦截该规则"
)


# ---------------------------------------------------------------------------
# 进程控制器
# ---------------------------------------------------------------------------
class ProcessController:
    """进程控制器 —— 守护者唯一允许碰的东西。

    这里有四个动作 —— 挂起（suspend）、恢复（resume）、中断（interrupt），
    以及**终止**（terminate）。

    ⚠️ v2.1.0 新增了 ``terminate``，这是红线 R6
    「哨兵绝不终止被监控进程」的**唯一例外**，而且限制极严：

    * 只对**哨兵自己挂起过**的 PID 生效（见
      :meth:`Guardian.terminate_suspended`）——
      别的进程它连碰都不碰；
    * 必须由**用户显式点击**【回滚并重试】触发；
    * 挂起态意味着进程正卡在一条**危险命令**上 ——
      放行它等于放行那条命令，所以「终止 + 重新拉起」才是安全解。

    前三个动作全部可逆；``terminate`` **不可逆**，
    所以它只出现在「用户已经确认过计划」的那条路径上。

    :param prefer: 首选动作 —— ``auto`` / ``suspend`` / ``sigint``。
    """

    #: 支持的动作名。
    ACTIONS = ("auto", "suspend", "sigint")

    def __init__(self, prefer: str = "auto") -> None:
        value = str(prefer or "auto").lower()
        self.prefer = value if value in self.ACTIONS else "auto"

    # -- 能力探测 -----------------------------------------------------
    @staticmethod
    def is_windows() -> bool:
        """当前是否运行在 Windows 上。"""
        return os.name == "nt"

    @staticmethod
    def psutil_module() -> Optional[Any]:
        """拿到 psutil 模块；没装就返回 ``None``。

        psutil 是**可选依赖** —— 缺了它守护者自动降级成「只报告、
        不动手」，绝不硬失败。
        """
        try:
            import psutil
        except ImportError:
            return None
        return psutil

    # -- 动作 ---------------------------------------------------------
    def suspend(self, pid: int) -> Dict[str, Any]:
        """挂起进程（之后可以 :meth:`resume` 恢复）。

        Windows 走 ``psutil.Process.suspend()``（NtSuspendProcess），
        Linux 走 ``SIGSTOP``（psutil 在 Linux 上内部就是这么做的），
        macOS 的 psutil 没有实现 —— 那时退回 ``SIGSTOP``。

        :param pid: 目标进程 PID。
        :return: 结果字典，字段见 :meth:`_result`。
        """
        result = self._result(False, "suspend", pid)
        psutil = self.psutil_module()
        if psutil is None:
            result["error"] = "psutil 未安装，无法挂起进程"
            return result
        try:
            process = psutil.Process(int(pid))
            process.suspend()
        except Exception as exc:  # noqa: BLE001 —— 动作失败只降级
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result["ok"] = True
        result["detail"] = "进程已挂起（随时可恢复）"
        return result

    def resume(self, pid: int) -> Dict[str, Any]:
        """恢复被挂起的进程。"""
        result = self._result(False, "resume", pid)
        psutil = self.psutil_module()
        if psutil is None:
            result["error"] = "psutil 未安装，无法恢复进程"
            return result
        try:
            process = psutil.Process(int(pid))
            process.resume()
        except Exception as exc:  # noqa: BLE001 —— 动作失败只降级
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result["ok"] = True
        result["detail"] = "进程已恢复运行"
        return result

    def terminate(self, pid: int) -> Dict[str, Any]:
        """结束进程（**不可逆**）。

        ⚠️ 这是红线 R6「绝不终止被监控进程」的**唯一例外**，
        只在【回滚并重试】这条**用户显式确认过计划**的路径上被调用。

        为什么需要它：被哨兵挂起的进程正卡在一条**危险命令**上，
        「放行」等于**放行那条命令** —— 那不是复活，那是闯祸。
        所以复活只能是「结束它 + 重新拉起一个带着纠偏提示的干净进程」。

        作用域由 :meth:`Guardian.terminate_suspended` 收紧：
        只对**哨兵自己挂起过**的 PID 生效。

        :param pid: 目标进程 PID。
        :return: 结果字典，字段见 :meth:`_result`。
        """
        result = self._result(False, "terminate", pid)
        psutil = self.psutil_module()
        if psutil is None:
            result["error"] = "psutil 未安装，无法结束进程"
            return result
        try:
            process = psutil.Process(int(pid))
            process.terminate()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                # 顽固进程：升级为 kill。
                process.kill()
                try:
                    process.wait(timeout=3)
                except psutil.TimeoutExpired as exc:
                    # 强杀后还等不到退出 —— 要留痕，
                    # 否则「进程没死透」这件事没人知道。
                    logger.warning(
                        "强杀后等待进程退出超时（PID %s）：%s",
                        process.pid,
                        exc,
                    )
        except Exception as exc:  # noqa: BLE001 —— 动作失败只降级
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result["ok"] = True
        result["detail"] = "进程已结束"
        return result

    def interrupt(self, pid: int) -> Dict[str, Any]:
        """发送中断信号（Ctrl+C 语义）。

        ⚠️ **Windows 上刻意不支持**：``GenerateConsoleCtrlEvent``
        作用于**整个控制台进程组**，一旦目标不是以
        ``CREATE_NEW_PROCESS_GROUP`` 创建的进程组组长，这个调用
        可能波及哨兵自身和用户正在跑的其它程序。
        那已经不是「干预」，那是「误伤」—— 所以宁可不做。

        POSIX 上 ``SIGINT`` 就是 Ctrl+C，Agent 有机会自己收尾。

        :param pid: 目标进程 PID。
        :return: 结果字典，字段见 :meth:`_result`。
        """
        result = self._result(False, "sigint", pid)
        if self.is_windows():
            result["error"] = (
                "Windows 上不对任意进程发送 Ctrl+C："
                "该调用会作用于整个控制台进程组，可能误伤用户程序"
            )
            return result
        try:
            os.kill(int(pid), signal.SIGINT)
        except (OSError, ProcessLookupError, PermissionError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result["ok"] = True
        result["detail"] = "已发送 SIGINT（Ctrl+C 语义）"
        return result

    def intervene(self, pid: int, action: str = "") -> Dict[str, Any]:
        """按策略执行一次干预。

        :param pid: 目标进程 PID。
        :param action: 覆盖本次使用的动作；留空用构造时的 ``prefer``。
        :return: 结果字典，字段见 :meth:`_result`。
        """
        chosen = str(action or self.prefer).lower()
        if chosen not in self.ACTIONS:
            chosen = "auto"
        if chosen == "auto":
            chosen = "suspend" if self.is_windows() else "sigint"

        if chosen == "sigint":
            if self.is_windows():
                outcome = self.suspend(pid)
                outcome["action"] = "suspend"
                outcome["detail"] = (
                    "Windows 不支持对任意进程发送 Ctrl+C，已降级为挂起。"
                    + outcome["detail"]
                )
                return outcome
            outcome = self.interrupt(pid)
            if outcome["ok"]:
                return outcome
            fallback = self.suspend(pid)
            fallback["action"] = "suspend"
            fallback["error"] = outcome["error"]
            fallback["detail"] = (
                f"SIGINT 失败（{outcome['error']}），已降级为挂起。"
                + fallback["detail"]
            )
            return fallback

        return self.suspend(pid)

    # -- 内部工具 -----------------------------------------------------
    @staticmethod
    def _result(ok: bool, action: str, pid: int) -> Dict[str, Any]:
        """构造统一形状的结果字典。"""
        return {
            "ok": bool(ok),
            "action": action,
            "pid": int(pid),
            "detail": "",
            "error": "",
        }


# ---------------------------------------------------------------------------
# 守护者
# ---------------------------------------------------------------------------
class Guardian:
    """守护者 —— 把「红线级」判定变成对目标进程的可逆干预。

    职责边界
    ========

    * **只处理红线**（``severity == "red_line"``），普通高危命令一律
      只告警 —— 守护者不是「更吵的告警器」；
    * **可被否决**：用户放行后，该规则在本次任务中不再被拦截；
    * **有留痕**：干预与放行都写进 ``logs/``；
    * **权限小于用户**：用户随时能放行，哨兵不能反过来。

    :param config: 已加载的配置字典（读 ``guardian`` 段与
        ``alert.intervention_mode``）。
    :param controller: 进程控制器；为 ``None`` 时自建。
    :param target_pid: 被监控进程的 PID；为 ``None`` 时按配置解析。
    :param on_event: 事件回调（终端 / GUI 用它把消息显示出来）。
    :param pid_resolver: 目标 PID 的**重新解析**回调，返回新 PID 或
        ``None``。用于「原 PID 已经没了」的场景 ——
        比如 Agent 自己重启过、或者启动器当初认错了进程。
        拦截失败（进程不存在）时会自动调它重试一次。
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        controller: Optional[ProcessController] = None,
        target_pid: Optional[int] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        pid_resolver: Optional[Callable[[], Optional[int]]] = None,
    ) -> None:
        self.config: Dict[str, Any] = config or {}
        guardian_config = self.config.get("guardian") or {}
        self.guardian_config: Dict[str, Any] = guardian_config
        alert_config = self.config.get("alert") or {}

        self.mode = str(
            alert_config.get("intervention_mode") or "advisory"
        ).lower()
        # 守护者必须显式开启 —— 权限不能默认比用户大。
        self.enabled = bool(guardian_config.get("enabled", False)) or (
            self.mode == "guardian"
        )
        self.on_event = on_event
        #: 目标 PID 的重新解析回调（可选）。
        self._pid_resolver = pid_resolver

        action = str(guardian_config.get("action") or "auto").lower()
        self.controller = controller or ProcessController(prefer=action)

        raw_pid = target_pid if target_pid is not None else guardian_config.get(
            "target_pid"
        )
        self._target_pid: Optional[int] = None
        if raw_pid:
            try:
                self._target_pid = int(raw_pid)
            except (TypeError, ValueError):
                self._target_pid = None
        self._pid_file = str(guardian_config.get("pid_file") or "").strip()

        #: 用户永久放行的规则（配置里的 ``allowed_rules``）。
        self._allowed_rules: Set[str] = {
            str(item).strip()
            for item in (guardian_config.get("allowed_rules") or [])
            if str(item).strip()
        }
        #: 用户本次任务放行的规则（override 产生）。
        self._overridden_rules: Set[str] = set()
        #: 被哨兵挂起、尚未恢复的 PID。
        self._suspended_pids: Set[int] = set()
        #: 最近一次干预记录（按规则 ID 索引）。
        self._interventions: Dict[str, Dict[str, Any]] = {}

        self._audit_path = os.path.abspath(
            str(
                guardian_config.get("audit_log")
                or os.path.join("logs", "guardian.log")
            )
        )
        self._override_path = os.path.abspath(
            str(
                guardian_config.get("override_log")
                or os.path.join("logs", "guardian_overrides.json")
            )
        )
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """对一条红线级事件执行干预。

        :param payload: 至少要有 ``rule_id``；可选 ``reason`` /
            ``command`` / ``pid`` / ``action``。
        :return: 干预结果字典，字段见 :meth:`_outcome`。
        """
        with self._lock:
            rule_id = str(payload.get("rule_id") or "")
            if not self.enabled:
                return self._outcome(
                    False,
                    "守护者未启用（需要 intervention_mode=guardian）",
                    rule_id=rule_id,
                )
            if not rule_id:
                return self._outcome(False, "缺少 rule_id，无法定位规则")

            if self.is_allowed(rule_id):
                return self._outcome(
                    False,
                    f"规则 {rule_id} 已被用户放行，本次不再拦截",
                    rule_id=rule_id,
                    skipped=True,
                )

            pid = self.resolve_pid(payload.get("pid"))
            if pid is None:
                return self._outcome(
                    False,
                    "无法确定被监控进程的 PID，未执行拦截",
                    rule_id=rule_id,
                )
            if self.is_protected_pid(pid):
                return self._outcome(
                    False,
                    f"PID {pid} 属于哨兵自身或系统进程，拒绝干预",
                    rule_id=rule_id,
                    pid=pid,
                )

            outcome = self.controller.intervene(
                pid, str(payload.get("action") or "")
            )
            # 原 PID 可能已经没了 —— Agent 自己重启过、或者启动器当初
            # 认错了进程。那就**重新解析一次**再试，
            # 别让红线就这么白白溜过去（权限还是在用户手里：可逆、可放行）。
            if not outcome.get("ok") and self._is_process_gone(outcome):
                fresh = self._re_resolve_pid()
                if (
                    fresh is not None
                    and fresh != pid
                    and not self.is_protected_pid(fresh)
                ):
                    pid = fresh
                    outcome = self.controller.intervene(
                        pid, str(payload.get("action") or "")
                    )
            record = {
                "action": "intervene",
                "timestamp": time.time(),
                "rule_id": rule_id,
                "pid": pid,
                "method": outcome.get("action", ""),
                "ok": bool(outcome.get("ok")),
                "reason": str(payload.get("reason") or ""),
                "command": str(payload.get("command") or "")[:500],
                "detail": str(outcome.get("detail") or ""),
                "error": str(outcome.get("error") or ""),
                "source": str(payload.get("source") or "engine"),
            }
            if outcome.get("ok"):
                self._suspended_pids.add(pid)
                self._interventions[rule_id] = record
            self._write_audit(record)

            message = INTERCEPT_MESSAGE
            if not outcome.get("ok"):
                message = (
                    f"[哨兵拦截失败] {rule_id}：{outcome.get('error') or '未知原因'}"
                )
            # ⚠️ 这里**不**再发 on_event —— 拦截提示会随告警一起输出
            # （SentinelEngine._emit_alert 负责打印），两处都发会让用户
            # 在终端看到同一句话两遍。放行事件没有告警伴随，才需要事件通知。
            return self._outcome(
                bool(outcome.get("ok")),
                message,
                rule_id=rule_id,
                pid=pid,
                method=str(outcome.get("action") or ""),
                detail=str(outcome.get("detail") or ""),
                error=str(outcome.get("error") or ""),
                record=record,
            )

    def override(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """用户放行 —— 解除挂起，并让该规则在本次任务中不再被拦截。

        :param payload: 可选 ``rule_id``（留空表示放行最近一次拦截）、
            ``pid``、``source``（``gui`` / ``stdin`` / ``api``）。
        :return: 放行结果字典。
        """
        data = payload or {}
        with self._lock:
            rule_id = str(data.get("rule_id") or "").strip()
            if not rule_id:
                rule_id = self._last_rule_id()
            if not rule_id:
                return self._outcome(False, "没有可放行的拦截记录")

            pid = self.resolve_pid(data.get("pid"))
            resumed = False
            resume_detail = ""
            if pid is not None and pid in self._suspended_pids:
                outcome = self.controller.resume(pid)
                resumed = bool(outcome.get("ok"))
                resume_detail = str(outcome.get("detail") or "")
                if resumed:
                    self._suspended_pids.discard(pid)

            self._overridden_rules.add(rule_id)
            record = {
                "action": "override",
                "timestamp": time.time(),
                "rule_id": rule_id,
                "pid": pid,
                "resumed": resumed,
                "reason": str(data.get("reason") or "用户已覆盖高危决策"),
                "source": str(data.get("source") or "user"),
                "detail": resume_detail,
            }
            self._write_audit(record)
            self._write_override_log(record)
            message = (
                f"[哨兵已放行] 规则 {rule_id} 已从本次任务中移除，"
                "哨兵不再拦截该规则"
            )
            self._emit(
                {
                    "type": "guardian",
                    "action": "override",
                    "rule_id": rule_id,
                    "pid": pid,
                    "ok": True,
                    "message": message,
                }
            )
            return self._outcome(
                True,
                message,
                rule_id=rule_id,
                pid=pid,
                resumed=resumed,
                detail=resume_detail,
                record=record,
            )

    def resume(self, pid: Optional[int] = None) -> Dict[str, Any]:
        """恢复被挂起的进程（不拉黑规则）。"""
        with self._lock:
            target = int(pid) if pid else self._target_pid
            if target is None:
                return self._outcome(False, "没有可恢复的进程")
            if target not in self._suspended_pids:
                return self._outcome(True, "该进程未被哨兵挂起", pid=target)
            outcome = self.controller.resume(target)
            if outcome.get("ok"):
                self._suspended_pids.discard(target)
            return self._outcome(
                bool(outcome.get("ok")),
                str(outcome.get("detail") or outcome.get("error") or ""),
                pid=target,
            )

    def terminate_suspended(self, pid: Optional[int] = None) -> Dict[str, Any]:
        """结束**哨兵自己挂起过**的进程。

        这是红线 R6 的唯一例外，所以作用域必须收得死死的：

        * 只认 ``self._suspended_pids`` 里的 PID ——
          哨兵没挂起过的进程，它**连碰都不碰**；
        * 保护 PID（0 / 1 / 哨兵自身 / 哨兵父进程）一律拒绝；
        * 写审计留痕（:meth:`_write_audit`）。

        :param pid: 目标 PID；``None`` 表示「所有被挂起的」。
        :return: 结果字典。
        """
        with self._lock:
            if pid is not None:
                targets = [int(pid)]
            else:
                targets = sorted(self._suspended_pids)
            if not targets:
                return self._outcome(False, "没有可结束的挂起进程")
            outcome = self._outcome(False, "")
            terminated: List[int] = []
            for target in targets:
                if target not in self._suspended_pids:
                    outcome = self._outcome(
                        True, "该进程不是哨兵挂起的，不去动它", pid=target
                    )
                    continue
                if self.is_protected_pid(target):
                    outcome = self._outcome(
                        False, "该 PID 受保护，拒绝结束", pid=target
                    )
                    continue
                result = self.controller.terminate(target)
                if result.get("ok"):
                    self._suspended_pids.discard(target)
                    terminated.append(target)
                    self._write_audit(
                        {
                            "timestamp": time.time(),
                            "action": "terminate_suspended",
                            "pid": target,
                            "rule_id": "",
                            "reason": "用户请求【回滚并重试】",
                            "source": "user",
                            "detail": str(result.get("detail") or ""),
                        }
                    )
                    outcome = self._outcome(
                        True, "挂起的进程已结束", pid=target
                    )
                else:
                    outcome = self._outcome(
                        False,
                        str(result.get("error") or "结束进程失败"),
                        pid=target,
                    )
            if terminated:
                outcome["terminated"] = terminated
            return outcome

    def status(self) -> Dict[str, Any]:
        """返回守护者当前状态（GUI 与终端共用）。"""
        with self._lock:
            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "action": self.controller.prefer,
                "target_pid": self._target_pid,
                "suspended_pids": sorted(self._suspended_pids),
                "overridden_rules": sorted(self._overridden_rules),
                "allowed_rules": sorted(self._allowed_rules),
                "interventions": list(self._interventions.values()),
                "audit_log": self._audit_path,
                "override_log": self._override_path,
                "psutil_available": (
                    ProcessController.psutil_module() is not None
                ),
            }

    def is_allowed(self, rule_id: str) -> bool:
        """某个规则当前是否已被放行。"""
        key = str(rule_id or "")
        return key in self._allowed_rules or key in self._overridden_rules

    def force_enable(self) -> None:
        """强制开启守护者（命令行 ``--guardian`` 用）。

        配置里 ``intervention_mode`` 不是 ``guardian`` 时，
        命令行仍然可以临时开一次 —— 但**不会**写回配置，
        下次启动还是按配置走。
        """
        self.enabled = True
        self.mode = "guardian"

    @staticmethod
    def _is_process_gone(outcome: Dict[str, Any]) -> bool:
        """判断这次拦截是不是因为「目标进程已经不存在」。

        只认进程消失这一种 —— 别的失败原因（比如权限不足）重试也没用。

        :param outcome: :meth:`ProcessController.intervene` 的结果。
        :return: 是「进程没了」返回 ``True``。
        """
        error = str(outcome.get("error") or "")
        return any(
            marker in error
            for marker in (
                "NoSuchProcess",
                "process PID not found",
                "进程不存在",
            )
        )

    def _re_resolve_pid(self) -> Optional[int]:
        """通过 ``pid_resolver`` 重新解析目标 PID。

        :return: 新 PID；没有回调 / 回调失败 / 回调没给出可用 PID 时
            返回 ``None``。
        """
        if self._pid_resolver is None:
            return None
        try:
            fresh = self._pid_resolver()
        except Exception:  # noqa: BLE001 —— 重新解析失败只降级
            return None
        if fresh is None:
            return None
        try:
            fresh = int(fresh)
        except (TypeError, ValueError):
            return None
        self._target_pid = fresh
        return fresh

    def resolve_pid(self, explicit: Any = None) -> Optional[int]:
        """解析被监控进程的 PID。

        顺序：显式传入 → 配置 ``target_pid`` → ``pid_file`` → 自动探测。

        :param explicit: 调用方显式给的 PID。
        :return: PID；解析不出来返回 ``None``。
        """
        if explicit is not None and str(explicit).strip() != "":
            try:
                return int(explicit)
            except (TypeError, ValueError) as exc:
                # 显式 PID 写错了 —— 往下走自动探测。
                logger.warning(
                    "显式 PID 不是整数（%s）：%s: %s",
                    explicit,
                    type(exc).__name__,
                    exc,
                )
        if self._target_pid:
            return self._target_pid
        if self._pid_file:
            pid = self._read_pid_file()
            if pid:
                self._target_pid = pid
                return pid
        return None

    def detect_pid(self, hint: str) -> Optional[int]:
        """按命令行特征反查被监控进程的 PID。

        哨兵读的是 Agent 的日志文件，而写这个文件的进程通常就在
        命令行里带着这个路径 —— 这是自动定位目标进程最稳的办法。

        :param hint: 特征串，通常是被监控日志的路径。
        :return: PID；找不到返回 ``None``。
        """
        psutil = ProcessController.psutil_module()
        if psutil is None or not hint:
            return None
        try:
            needle = os.path.abspath(hint).lower()
        except (TypeError, ValueError):
            return None
        try:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                info = proc.info or {}
                cmdline = info.get("cmdline") or []
                joined = " ".join(str(part) for part in cmdline).lower()
                if needle and needle in joined:
                    pid = int(info.get("pid") or 0)
                    if pid and not self.is_protected_pid(pid):
                        return pid
        except Exception as exc:  # noqa: BLE001 —— 探测失败只降级
            logger.warning("探测目标进程失败：%s: %s", type(exc).__name__, exc)
        return None

    def set_target_pid(self, pid: Optional[int]) -> None:
        """记住被监控进程的 PID（自动探测成功后由引擎调用）。

        :param pid: PID；``None`` 表示放弃。
        """
        if pid is None:
            return
        try:
            self._target_pid = int(pid)
        except (TypeError, ValueError):
            logger.warning("无效的目标 PID：%r", pid)

    def is_protected_pid(self, pid: int) -> bool:
        """判断 PID 是否属于「绝对不能动」的那一类。

        PID 0 / 1 是系统的；哨兵自身与哨兵的父进程更是绝对不能碰 ——
        把自己挂起是一种很别致的自杀方式。
        """
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return True
        if value <= 1:
            return True
        if value == os.getpid():
            return True
        return value == os.getppid()

    def watch_stdin(self, stream: Any = None) -> Optional[threading.Thread]:
        """启动后台线程监听终端输入的 ``override``。

        纯终端用户没有 GUI 可点 —— 这条通道保证他们**同样拥有**
        否决权。输入不是交互式终端时直接跳过（不阻塞进程退出）。

        :param stream: 输入流；默认 ``sys.stdin``。
        :return: 线程实例；未启动时返回 ``None``。
        """
        source = stream if stream is not None else sys.stdin
        if source is None:
            return None
        isatty = getattr(source, "isatty", None)
        try:
            if isatty is None or not isatty():
                return None
        except (ValueError, OSError):
            return None
        thread = threading.Thread(
            target=self._stdin_loop,
            args=(source,),
            name="sentinel-guardian-stdin",
            daemon=True,
        )
        thread.start()
        return thread

    def close(self) -> None:
        """释放守护者持有的资源（当前无长连接，仅留作对称接口）。"""
        with self._lock:
            for pid in list(self._suspended_pids):
                try:
                    self.controller.resume(pid)
                except Exception as exc:  # noqa: BLE001 —— 释放失败只记日志
                    logger.warning(
                        "恢复挂起的进程 %s 失败：%s: %s",
                        pid,
                        type(exc).__name__,
                        exc,
                    )
            self._suspended_pids.clear()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _stdin_loop(self, stream: Any) -> None:
        """终端输入循环。"""
        for line in stream:
            text = str(line or "").strip().lower()
            if text in ("override", "放行"):
                self.override({"source": "stdin"})
            elif text in ("status", "状态"):
                self._emit(
                    {
                        "type": "guardian",
                        "action": "status",
                        "message": str(self.status()),
                    }
                )

    def _read_pid_file(self) -> Optional[int]:
        """从 ``pid_file`` 读取 PID。"""
        if not self._pid_file:
            return None
        try:
            with open(self._pid_file, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError) as exc:
            logger.warning(
                "读取 PID 文件失败 %s：%s: %s",
                self._pid_file,
                type(exc).__name__,
                exc,
            )
            return None

    def _last_rule_id(self) -> str:
        """最近一次被拦截的规则 ID。"""
        if not self._interventions:
            return ""
        return next(reversed(self._interventions))

    def _emit(self, event: Dict[str, Any]) -> None:
        """把事件交给回调（终端 / GUI 显示用）。"""
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:  # noqa: BLE001 —— 回调失败不影响守护者
            logger.warning("守护者事件回调失败：%s: %s", type(exc).__name__, exc)

    def _write_audit(self, record: Dict[str, Any]) -> None:
        """把干预 / 放行写进审计日志。"""
        line = (
            "{stamp}  {action:<9}  rule={rule}  pid={pid}  ok={ok}  "
            "source={source}  {reason}"
        ).format(
            stamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            action=str(record.get("action") or ""),
            rule=str(record.get("rule_id") or "-"),
            pid=str(record.get("pid") if record.get("pid") else "-"),
            ok="1" if record.get("ok", record.get("resumed")) else "0",
            source=str(record.get("source") or "-"),
            reason=str(record.get("reason") or record.get("detail") or ""),
        )
        try:
            os.makedirs(os.path.dirname(self._audit_path), exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("写守护者审计日志失败：%s: %s", type(exc).__name__, exc)

    def _write_override_log(self, record: Dict[str, Any]) -> None:
        """把「用户已覆盖高危决策」写进结构化留痕文件。"""
        payload: Dict[str, Any] = {"version": 1, "decisions": []}
        try:
            if os.path.exists(self._override_path):
                with open(
                    self._override_path, "r", encoding="utf-8"
                ) as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    payload = loaded
        except (OSError, ValueError) as exc:
            logger.warning(
                "读放行留痕文件失败，将新建：%s: %s", type(exc).__name__, exc
            )

        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            decisions = []
        entry = dict(record)
        entry["stamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        decisions.append(entry)
        payload["decisions"] = decisions

        try:
            os.makedirs(os.path.dirname(self._override_path), exist_ok=True)
            with open(
                self._override_path, "w", encoding="utf-8"
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("写放行留痕文件失败：%s: %s", type(exc).__name__, exc)

    @staticmethod
    def _outcome(
        ok: bool,
        message: str,
        *,
        rule_id: str = "",
        pid: Optional[int] = None,
        method: str = "",
        detail: str = "",
        error: str = "",
        skipped: bool = False,
        resumed: bool = False,
        record: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构造统一形状的返回字典（GUI 与终端都读它）。"""
        return {
            "ok": bool(ok),
            "message": message,
            "rule_id": rule_id,
            "pid": pid,
            "method": method,
            "detail": detail,
            "error": error,
            "skipped": bool(skipped),
            "resumed": bool(resumed),
            "record": record or {},
        }


def load_guardian(
    config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Guardian:
    """按配置构造守护者（便捷入口）。

    :param config: 已加载的配置字典。
    :param kwargs: 其余参数透传给 :class:`Guardian`。
    :return: 守护者实例。
    """
    return Guardian(config, **kwargs)
