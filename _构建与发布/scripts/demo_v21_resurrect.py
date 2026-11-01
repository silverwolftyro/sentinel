"""v2.1.0 一键复活演示 —— 起一个**真能复活**的看板。

这个脚本造一个**完整且真实**的现场：

* 工作目录里有 Agent 改坏的文件和新建的垃圾文件；
* 有一份基线安全点（改坏之前的快照）；
* 有一条真实的红线级告警，**带着安全点信息**；
* **有一个真的被挂起的子进程**（``psutil`` 能读出 ``STATUS_STOPPED``）；
* 看板上的【查看改动】/【一键回滚至拦截点】/【回滚并重试】都能真的干活。

「回滚并重试」会真的：回滚文件 → 写留言箱 → **结束挂起的进程** →
**重新拉起一个新进程**（新进程会把留言箱里的纠偏提示打进任务提示里）。

用法::

    python _构建与发布/scripts/demo_v21_resurrect.py --port 8285

脚本会一直跑着（看板在前台），按 Ctrl+C 结束。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)))

from src.core.guardian import Guardian  # noqa: E402
from src.core.injector import AgentInjector, InboxStore  # noqa: E402
from src.core.resurrect import ResurrectService  # noqa: E402
from src.core.safepoint import SafePointService  # noqa: E402

#: 假 Agent 的源码 —— 它会打印一行 JSONL（像真的 CLI），然后一直活着。
FAKE_AGENT_SOURCE = '''\
import json
import os
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
task = sys.argv[1] if len(sys.argv) > 1 else ""
print(json.dumps({"type": "user_message", "content": task},
                 ensure_ascii=False), flush=True)
print(json.dumps({
    "type": "agent_message",
    "content": "收到任务：" + task[:120],
}, ensure_ascii=False), flush=True)
sys.stdout.flush()
while True:
    time.sleep(0.5)
'''


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


class _AgentControl:
    """假 Agent 的进程控制 —— 真的起进程、真的结束进程。

    它就是 :mod:`src.core.resurrect` 说的那个「鸭子类型」：
    ``state()`` / ``terminate()`` / ``relaunch()``。
    """

    def __init__(self, script: str, workdir: str, inbox: InboxStore) -> None:
        self.script = script
        self.workdir = workdir
        self.inbox = inbox
        self.proc: Optional[subprocess.Popen] = None
        self.generation = 0
        self.lock = threading.Lock()
        #: 守护者（用来问「现在是不是挂起状态」）。
        self.guardian: Optional[Guardian] = None

    def _spawn(self, task: str) -> subprocess.Popen:
        """起一个新进程，并把任务提示打给它。"""
        proc = subprocess.Popen(
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
        self.generation += 1
        self.proc = proc
        return proc

    def state(self) -> Dict[str, Any]:
        """当前进程状态（挂起与否问**真的**守护者）。"""
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
        """重新拉起一个进程（任务提示里带上留言箱里的纠偏提示）。"""
        with self.lock:
            block = self.inbox.render_for_prompt(limit=3)
            task = (
                "【哨兵纠偏提示】\n" + str(prompt or "")[:400] + "\n\n"
                + block
            ) if block else str(prompt or "")
            proc = self._spawn(task)
            return {
                "ok": True,
                "pid": proc.pid,
                "detail": "已重新拉起 Agent。",
                "task_preview": task[:200],
            }


def _build_scene(workdir: str) -> Dict[str, Any]:
    """造出「Agent 把代码改乱了」的现场。

    :param workdir: 现场目录。
    :return: 场景信息字典。
    """
    project = os.path.join(workdir, "project")
    os.makedirs(os.path.join(project, "src"), exist_ok=True)
    app_path = os.path.join(project, "src", "app.py")
    with open(app_path, "w", encoding="utf-8") as handle:
        handle.write(
            "def main():\n"
            "    # 正常版本：返回 'good'\n"
            "    return 'good'\n"
        )
    with open(os.path.join(project, "junk.tmp"), "w", encoding="utf-8") as fh:
        fh.write("临时文件\n")

    # --- 基线安全点：Agent 动手之前 ---
    service = SafePointService(root=os.path.join(workdir, "safepoints"))
    baseline = service.capture(project, label="基线安全点 · 会话起点")
    print(f"[1/4] 基线安全点：{baseline.id}"
          f"（{baseline.files_count} 个文件，{baseline.elapsed:.3f} 秒）")

    # --- Agent 把代码改乱 ---
    with open(app_path, "w", encoding="utf-8") as handle:
        handle.write(
            "def main():\n"
            "    # Agent 改坏的版本\n"
            "    return 'broken'\n"
        )
    with open(
        os.path.join(project, "src", "scratch.py"), "w", encoding="utf-8"
    ) as handle:
        handle.write("# Agent 新建的垃圾文件\n")
    print("[2/4] Agent 已把 src/app.py 改坏，并新建了 src/scratch.py")

    # --- 拦截点快照（挂起前打的那一张）---
    point = service.capture(project, label="红线拦截快照")
    print(f"[3/4] 拦截点快照：{point.id}"
          f"（{point.files_count} 个文件，{point.elapsed:.3f} 秒）")

    return {
        "project": project,
        "service": service,
        "baseline": baseline,
        "point": point,
        "app_path": app_path,
    }


def _write_alert(alert_log: str, scene: Dict[str, Any]) -> str:
    """往告警日志里写一条**带安全点**的红线级告警。

    :param alert_log: 告警日志路径。
    :param scene: 场景信息。
    :return: 告警 ID。
    """
    alert_id = "1793094000000-000001"
    payload = {
        "alert_id": alert_id,
        "timestamp": time.time(),
        "verdict": "DANGEROUS",
        "level": "critical",
        "rule_id": "catastrophic_mass_delete",
        "reason": "红线级命中：用 find 批量删除文件（find . -name '*.tmp' -delete）",
        "evidence": "find . -name '*.tmp' -delete",
        "severity": "red_line",
        "category": "mass_delete",
        "source": "demo",
        "event_type": "tool_call",
        "tool": "Bash",
        "command": "find . -name '*.tmp' -delete",
        "arguments": {"command": "find . -name '*.tmp' -delete"},
        "explanation": (
            "AI 想批量删除项目里所有临时文件。影响：当前目录及其所有"
            "子目录中所有以 .tmp 结尾的文件（会直接删除，不经过回收站）。"
        ),
        "explanation_degraded": False,
        "guardian": {"ok": True, "action": "suspend", "pid": 12345},
        "safe_point": {
            **scene["point"].to_dict(),
            "rollback_point": scene["baseline"].to_dict(),
            "rollback_label": scene["baseline"].label,
        },
    }
    os.makedirs(os.path.dirname(alert_log), exist_ok=True)
    with open(alert_log, "a", encoding="utf-8") as handle:
        handle.write(
            "2026-10-27 18:40:00,000 WARNING ALERT_JSON "
            + json.dumps(payload, ensure_ascii=False)
            + "\n"
        )
    return alert_id


def main() -> int:
    """起演示看板。"""
    parser = argparse.ArgumentParser(description="v2.1.0 一键复活演示")
    parser.add_argument("--port", type=int, default=8285)
    parser.add_argument(
        "--workdir",
        default="",
        help="现场目录（留空 = 用 logs/v21_demo）",
    )
    args = parser.parse_args()

    workdir = os.path.abspath(
        args.workdir
        or os.path.join(PROJECT_ROOT, "logs", "v21_demo")
    )
    os.makedirs(workdir, exist_ok=True)
    project = os.path.join(workdir, "project")

    print("=" * 66)
    print("  哨兵 v2.1.0 · 一键复活演示")
    print("=" * 66)
    scene = _build_scene(workdir)
    service = scene["service"]

    # --- 假 Agent 脚本 ---
    script = os.path.join(workdir, "fake_agent.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(FAKE_AGENT_SOURCE)

    inbox = InboxStore(os.path.join(project, "sentinel_inbox.json"))
    control = _AgentControl(script, project, inbox)

    # --- 起一个真的 Agent 进程，并真的挂起它 ---
    proc = control._spawn("修好 src/app.py 里的 bug")
    time.sleep(0.6)
    guardian = Guardian(
        {"guardian": {"enabled": True, "action": "suspend"}},
        target_pid=proc.pid,
    )
    outcome = guardian.intervene({
        "rule_id": "catastrophic_mass_delete",
        "reason": "红线级命中：用 find 批量删除文件",
        "command": "find . -name '*.tmp' -delete",
        "pid": proc.pid,
    })
    time.sleep(0.3)
    status = ""
    try:
        import psutil

        status = psutil.Process(proc.pid).status()
    except Exception:  # noqa: BLE001 —— psutil 缺失也不该挡住演示
        status = "(读不到)"
    print(f"[4/4] Agent 进程 PID={proc.pid} 已被守护者挂起"
          f"（psutil 状态：{status}；拦截 ok={outcome.get('ok')}）")
    control.guardian = guardian

    alert_log = os.path.join(workdir, "sentinel_alert.log")
    watch_log = os.path.join(workdir, "agent.jsonl")
    with open(watch_log, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user_message",
                    "content": "修好 src/app.py 里的 bug",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    alert_id = _write_alert(alert_log, scene)
    print(f"告警已就位：{alert_id}")

    # --- 看板 ---
    import uvicorn

    from src.gui.server import SessionSpec, create_app

    def _inject(prompt: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """看板的【直接发送给 Agent】回调。"""
        return AgentInjector(inbox=inbox, agent_label="demo").inject(
            prompt,
            session_id="demo",
            verdict=str((meta or {}).get("verdict") or ""),
            rule_id=str((meta or {}).get("rule_id") or ""),
            reason=str((meta or {}).get("reason") or ""),
        ).to_dict()

    def _rollback(point_id: str = "", workdir_: str = "") -> Dict[str, Any]:
        """看板的【一键回滚至拦截点】回调。"""
        return service.restore(point_id, workdir_ or project)

    def _resume(prompt: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """看板的【回滚并重试】回调 —— 真的回滚 + 注入 + 重拉。"""
        result = _resurrect().execute(prompt, meta)
        if result.get("ok"):
            print(f"  {result.get('message')}")
            for step in result.get("steps") or []:
                print(f"    · {step}")
        return result

    def _resume_plan(point_id: str = "", workdir_: str = "") -> Dict[str, Any]:
        """看板的【复活计划】回调 —— 只算不动。"""
        return _resurrect().plan(point_id, workdir_ or project)

    def _resurrect() -> ResurrectService:
        """造一个复活服务。"""
        return ResurrectService(
            safe_point_service=service,
            # ⚠️ 只走留言箱 —— 旧进程马上就要被结束了。
            injector=AgentInjector(inbox=inbox, agent_label="demo"),
            guardian=guardian,
            agent_control=control,
            label="demo",
        )

    spec = SessionSpec(
        id="demo",
        label="演示会话",
        agent="demo",
        watch_path=watch_log,
        alert_log_path=alert_log,
        inject_callback=_inject,
        rollback_callback=_rollback,
        resume_callback=_resume,
        plan_callback=_resume_plan,
        safe_point_service=service,
        workdir=project,
    )
    app = create_app(sessions=[spec], host="127.0.0.1", port=args.port)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning"
    )
    server = uvicorn.Server(config)
    threading.Thread(
        target=server.run, name="demo-dashboard", daemon=True
    ).start()

    print()
    print("=" * 66)
    print(f"  看板：http://127.0.0.1:{args.port}/")
    print(f"  现场：{workdir}")
    print("  试试看：")
    print("    · 【查看改动】→ 弹窗里看 Agent 到底改了哪些文件")
    print("    · 【一键回滚至拦截点】→ src/app.py 恢复成 good")
    print("    · 【回滚并重试】→ 回滚 + 注入 + 结束挂起进程 + 重拉 Agent")
    print("  按 Ctrl+C 结束。")
    print("=" * 66)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n结束演示。")
    finally:
        try:
            if control.proc is not None:
                control.terminate()
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不影响退出
            print(
                "收尾结束 Agent 进程失败：{}: {}".format(
                    type(exc).__name__, exc
                ),
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
