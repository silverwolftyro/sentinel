"""v2.1.0 晨报归档 —— 跑一遍真实会话，把晨报**归档**进管理文档。

这个脚本做一件**完整**的事：

1. 造一个真实的测试工作目录；
2. 起**真实**的哨兵引擎（规则层 + 守护者 + 安全点 + 通知器）；
3. 流式喂一段事件：Agent 卡死几次 → 改坏文件 → 尝试高危删除；
4. 守护者**真的**拦截（挂起进程）+ **真的**打安全点；
5. 走一遍**一键复活**：回滚 → 注入 → 结束挂起进程 → 重拉；
6. 算一遍 **Web Diff**；
7. 从引擎统计生成《哨兵值守晨报》，**归档**到
   ``_管理文档/文档与报告/晨报归档/晨报-YYYY-MM-DD.md``。

⚠️ 归档用的是**重建式合并** —— 同一天的多路会话各占一节，
   不会互相覆盖。

用法::

    python _构建与发布/scripts/v21_briefing_demo.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)))

from src.adapters.mcode import McodeAdapter  # noqa: E402
from src.core.briefing import (  # noqa: E402
    collect_from_engine,
    render_markdown,
    render_terminal,
    save_briefing,
)
from src.core.engine import ContextTracker, SentinelEngine  # noqa: E402
from src.core.guardian import Guardian  # noqa: E402
from src.core.injector import AgentInjector, InboxStore  # noqa: E402
from src.core.notifier import Notifier  # noqa: E402
from src.core.resurrect import ResurrectService  # noqa: E402
from src.core.safepoint import SafePointService  # noqa: E402
from src.rules.basic import RuleEngine  # noqa: E402

#: 会话 ID —— 归档时用它占一节。
SESSION_ID = "v21-control"


def _find_project_root() -> str:
    """向上找到项目根目录（含 ``src/core`` 的那一层）。"""
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(current, "src", "core")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = _find_project_root()


def _event_user(text: str) -> str:
    """一条用户消息。"""
    return json.dumps(
        {"type": "user_message", "content": text}, ensure_ascii=False
    )


def _event_tool_call(name: str, command: str) -> str:
    """一条工具调用。"""
    return json.dumps(
        {
            "type": "tool_call",
            "tool_call": {"name": name, "arguments": {"command": command}},
        },
        ensure_ascii=False,
    )


def _event_tool_result_error(output: str) -> str:
    """一条**失败**的工具回执。"""
    return json.dumps(
        {"type": "tool_result", "tool_result": {"status": "error", "output": output}},
        ensure_ascii=False,
    )


def _event_tool_result_ok(output: str) -> str:
    """一条成功的工具回执。"""
    return json.dumps(
        {"type": "tool_result", "tool_result": {"status": "ok", "output": output}},
        ensure_ascii=False,
    )


def _build_config(workdir: str, target_pid: int) -> Dict[str, Any]:
    """造一份配置（守护者武装 + 安全点开启 + 留言箱开启）。"""
    return {
        "guardian": {
            "enabled": True,
            "mode": "guardian",
            "action": "suspend",
            "target_pid": target_pid,
            "audit_log": os.path.join(workdir, "guardian.log"),
            "override_log": os.path.join(workdir, "guardian_overrides.json"),
        },
        "alert": {
            "enabled": True,
            "min_level": "info",
            "intervention_mode": "guardian",
            "merge_seconds": 60,
            "channels": [
                {
                    "type": "file",
                    "enabled": True,
                    "path": os.path.join(workdir, "alerts.jsonl"),
                }
            ],
        },
        "safe_point": {
            "enabled": True,
            "root": os.path.join(workdir, "safepoints"),
            "max_bytes": 64 * 1024 * 1024,
            "max_files": 20000,
            "max_seconds": 3.0,
            "recent_minutes": 30,
        },
        "intervention": {
            "enabled": True,
            "inbox_name": "sentinel_inbox.json",
            "max_inbox_messages": 3,
        },
        "logging": {"level": "INFO", "dir": workdir},
    }


class _AgentControl:
    """假 Agent 的进程控制（真的起进程、真的结束进程）。"""

    def __init__(self, script: str, workdir: str, inbox: InboxStore) -> None:
        self.script = script
        self.workdir = workdir
        self.inbox = inbox
        self.proc: Any = None
        self.guardian: Any = None

    def state(self) -> Dict[str, Any]:
        """当前进程状态。"""
        proc = self.proc
        suspended = False
        if self.guardian is not None and proc is not None:
            try:
                status = self.guardian.status()
                suspended = bool(
                    proc.pid in (status.get("suspended_pids") or [])
                )
            except Exception:  # noqa: BLE001 —— 查询失败只降级
                suspended = False
        return {
            "ok": True,
            "pid": proc.pid if proc is not None else 0,
            "alive": proc is not None and proc.poll() is None,
            "suspended": suspended,
        }

    def terminate(self) -> Dict[str, Any]:
        """结束当前进程。"""
        proc = self.proc
        if proc is None:
            return {"ok": False, "detail": "没有进程可结束。"}
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001 —— 结束失败只降级
            print(
                "结束进程失败（PID {}）：{}: {}".format(
                    proc.pid, type(exc).__name__, exc
                ),
                file=sys.stderr,
            )
        return {"ok": True, "detail": "进程已结束。"}

    def relaunch(self, prompt: str = "") -> Dict[str, Any]:
        """重新拉起一个进程（任务提示里带上留言箱的纠偏提示）。"""
        block = self.inbox.render_for_prompt(limit=3)
        task = (
            "【哨兵纠偏提示】\n" + str(prompt or "")[:400] + "\n\n" + block
        ) if block else str(prompt or "")
        self.proc = subprocess.Popen(
            [sys.executable, self.script, task],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self.workdir,
        )
        return {
            "ok": True,
            "pid": self.proc.pid,
            "detail": "已重新拉起 Agent。",
            "task_preview": task[:200],
        }


FAKE_AGENT = '''\
import json
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
task = sys.argv[1] if len(sys.argv) > 1 else ""
print(json.dumps({"type": "user_message", "content": task},
                 ensure_ascii=False), flush=True)
sys.stdout.flush()
while True:
    time.sleep(0.5)
'''


def main() -> int:
    """跑一遍完整流程并归档晨报。"""
    workdir = os.path.abspath(
        os.path.join(PROJECT_ROOT, "logs", "v21_briefing")
    )
    os.makedirs(workdir, exist_ok=True)
    project = os.path.join(workdir, "project")
    os.makedirs(os.path.join(project, "src"), exist_ok=True)

    app_path = os.path.join(project, "src", "app.py")
    with open(app_path, "w", encoding="utf-8") as handle:
        handle.write("def main():\n    return 'good'\n")

    script = os.path.join(workdir, "fake_agent.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(FAKE_AGENT)

    inbox = InboxStore(os.path.join(project, "sentinel_inbox.json"))
    control = _AgentControl(script, project, inbox)
    agent = subprocess.Popen(
        [sys.executable, script, "修好 src/app.py 里的 bug"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=project,
    )
    control.proc = agent

    config = _build_config(workdir, agent.pid)
    guardian = Guardian(config, target_pid=agent.pid)
    control.guardian = guardian
    notifier = Notifier(config)
    safe_points = SafePointService(
        root=os.path.join(workdir, "safepoints"), max_seconds=3.0
    )
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config),
        alert_log_path=os.path.join(workdir, "sentinel_alert.log"),
        console=False,
        llm_client=None,
        context_tracker=ContextTracker(window=5, stuck_threshold=3),
        intent_tracker=None,
        notifier=notifier,
        guardian=guardian,
        agent_name="v21-demo",
        safe_point_service=safe_points,
        workdir=project,
    )

    log_path = os.path.join(workdir, "agent.jsonl")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("")

    def _run_engine() -> None:
        try:
            engine.run(log_path, tail=True)
        except Exception as exc:  # noqa: BLE001 —— 失败要报出来
            print(f"引擎异常：{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=_run_engine, daemon=True)
    worker.start()
    time.sleep(1.5)  # 等基线安全点打好

    print("=" * 66)
    print("  哨兵 v2.1.0 · 晨报归档演示")
    print("=" * 66)
    print(f"工作目录：{project}")

    # --- Agent 把代码改坏 ---
    with open(app_path, "w", encoding="utf-8") as handle:
        handle.write("def main():\n    return 'broken'\n")
    with open(
        os.path.join(project, "src", "scratch.py"), "w", encoding="utf-8"
    ) as handle:
        handle.write("# Agent 新建的垃圾文件\n")
    print("[1/5] Agent 已把 src/app.py 改坏，并新建了 src/scratch.py")

    # --- 喂事件：卡死几次，然后高危删除 ---
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(_event_user("修好 src/app.py 里的 bug") + "\n")
        for _ in range(3):
            handle.write(
                _event_tool_call("Bash", "pytest tests/test_fix.py")
                + "\n"
            )
            handle.write(
                _event_tool_result_error("AssertionError: 期望 'good' 实际 'broken'")
                + "\n"
            )
        handle.write(
            _event_tool_call("Bash", "find . -name '*.tmp' -delete") + "\n"
        )
        handle.flush()
    time.sleep(2.5)
    print("[2/5] 已触发 STUCK + 红线级高危删除")

    # --- Web Diff ---
    diff = safe_points.diff(workdir=project)
    print(
        "[3/5] Web Diff：修改 {} 个、新增 {} 个、删除 {} 个".format(
            diff.get("modified_count", 0),
            diff.get("added_count", 0),
            diff.get("removed_count", 0),
        )
    )
    for detail in diff.get("files") or []:
        print(f"      · {detail['path']}（+{detail['added']} / -{detail['removed']}）")

    # --- 一键复活 ---
    resurrect = ResurrectService(
        safe_point_service=safe_points,
        injector=AgentInjector(inbox=inbox, agent_label="v21-demo"),
        guardian=guardian,
        agent_control=control,
        label=SESSION_ID,
    )
    prompt = (
        "【哨兵纠偏提示 · DANGEROUS】\n"
        "红线级命中：用 find 批量删除文件。\n"
        "请改用安全的方式清理临时文件，不要批量删除。"
    )
    result = resurrect.execute(prompt, {"verdict": "DANGEROUS"})
    print(f"[4/5] 一键复活：{result.get('message')}")
    for step in result.get("steps") or []:
        print(f"      · {step}")
    print(
        "      src/app.py 现在是："
        + open(app_path, encoding="utf-8").read().strip()
    )

    # --- 晨报 ---
    stats = collect_from_engine(
        engine, session_id=SESSION_ID, agent="v21-demo"
    )
    markdown = render_markdown(
        stats,
        # 归档里的每日晨报是**分节**的 —— 标题层级跟启动器保持一致，
        # 不然同一个文件里会出现两套标题层级。
        title=f"会话 {SESSION_ID} · v21-demo",
        heading_level=2,
    )
    # ⚠️ **必须**传 session_id —— 不传就是「单路模式」，
    # 会把同一天的日报**整个覆盖掉**（v1.2.0 那两节就没了）。
    path = save_briefing(markdown, archive_dir=None, session_id=SESSION_ID)
    print()
    print(render_terminal(stats))
    print(f"[5/5] 晨报已归档：{path}")

    # --- 收尾 ---
    try:
        agent.terminate()
        agent.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
        print(
            "结束演示 Agent 进程失败：{}: {}".format(
                type(exc).__name__, exc
            ),
            file=sys.stderr,
        )
    try:
        engine.close()
    except Exception as exc:  # noqa: BLE001
        print(
            "关闭引擎失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
