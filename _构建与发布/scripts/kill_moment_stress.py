"""「猎杀时刻」压力测试（v2.0.0）。

模拟一个**彻底失控**的 Agent：它在测试目录里反复跑同一个必失败的测试，
连续触发 5 次 ``[STUCK]``，中间还顺手执行了一条高危批量删除命令。

要验证的七件事
==============

====== ==========================================================
编号    验证内容
====== ==========================================================
C1     告警**顺序**：先 5 次 STUCK，红线级 DANGEROUS 在中间
C2     红线级**真的挂起了**被监控进程（不是只报个警）
C3     挂起**之前**打了安全点快照
C4     【一键回滚至拦截点】能把改坏的代码退回去
C5     **合并推送**：6 条告警只推 2~3 次，不骚扰
C6     指令注入：stdin 直写 + 留言箱降级 + 重启注入
C7     记录型通道**一条不少**（流水账不能因为合并而少记）
====== ==========================================================

脚本自始至终只操作**自己创建的**进程与目录，绝不碰用户的任何东西；
日志里那条高危命令**只作为文本出现，哨兵从不执行它**。

用法::

    python _构建与发布/scripts/kill_moment_stress.py
    python _构建与发布/scripts/kill_moment_stress.py --keep  # 保留现场
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)))

import yaml  # noqa: E402


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

#: 必失败的测试命令 —— Agent 每次都跑**同一条**命令。
#:
#: 命令相同 → 指纹相同 → 通知层才会合并。
#: 这正是真实场景：Agent 卡住时会**反复重试同一条命令**。
STUCK_COMMAND = "python -m pytest tests/test_kill.py -q"

#: 中间插进来的高危命令（红线级）。
#:
#: ⚠️ 它**只作为文本写进日志**，哨兵只读日志、从不执行它。
DANGEROUS_COMMAND = "find . -name '*.tmp' -delete"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
class _Result:
    """检查结果收集器。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def check(self, code: str, title: str, ok: bool, detail: str = "") -> bool:
        """记一条检查结果。"""
        self.rows.append(
            {"code": code, "title": title, "ok": bool(ok), "detail": detail}
        )
        mark = "通过" if ok else "**失败**"
        print(f"  [{code}] {title} —— {mark}")
        if detail:
            print(f"         {detail}")
        return bool(ok)

    @property
    def failed(self) -> List[Dict[str, Any]]:
        """失败的检查项。"""
        return [row for row in self.rows if not row["ok"]]


def _mock_webhook(workdir: str) -> Dict[str, Any]:
    """起一个本地假 Webhook，把收到的推送记下来。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received: List[Dict[str, Any]] = []
    lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        """只收 POST，不说多余的话。"""

        def do_POST(self) -> None:  # noqa: N802 —— 协议约定
            """接收一份推送报文。"""
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"_raw": raw}
            with lock:
                received.append({"body": body, "at": time.time()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args: Any) -> None:
            """静音 —— 演练输出要干净。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    address = server.server_address
    thread = threading.Thread(
        target=server.serve_forever, name="mock-webhook", daemon=True
    )
    thread.start()
    return {
        "url": f"http://127.0.0.1:{address[1]}/sentinel",
        "received": received,
        "stop": server.shutdown,
        "close": server.server_close,
    }


def _base_config(workdir: str, webhook_url: str) -> Dict[str, Any]:
    """构造演练用的配置。"""
    return {
        "rules": {
            "enabled": True,
            "catastrophic": {
                "enabled": True,
                "categories": [
                    "root_delete",
                    "mass_delete",
                    "disk_wipe",
                    "remote_exec",
                ],
            },
        },
        # 本次演练**不**调模型 —— 判定要完全确定，不能看模型心情。
        "llm": {"enabled": False},
        "alert": {
            "enabled": True,
            "min_level": "info",
            # 合并窗口：60 秒内同一件事只敲一次门。
            "merge_seconds": 60,
            "channels": [
                {
                    "type": "file",
                    "enabled": True,
                    "path": os.path.join(workdir, "alerts.jsonl"),
                },
                {"type": "desktop", "enabled": False},
                {"type": "sound", "enabled": False},
                {
                    "type": "webhook",
                    "enabled": True,
                    "url": webhook_url,
                    "format": "generic",
                    "dedup_seconds": 0,
                },
            ],
            "intervention_mode": "guardian",
        },
        "guardian": {
            "enabled": True,
            "target_pid": 0,
            "action": "auto",
            "allowed_rules": [],
            "audit_log": os.path.join(workdir, "guardian.log"),
            "override_log": os.path.join(workdir, "guardian_overrides.json"),
        },
        "context": {"enabled": True, "window": 5, "stuck_threshold": 3},
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


def _event_user(text: str) -> str:
    """一条用户消息。"""
    return json.dumps(
        {"type": "user_message", "content": text}, ensure_ascii=False
    )


def _event_thinking(text: str) -> str:
    """一条思考。"""
    return json.dumps(
        {"type": "thinking", "thinking": text}, ensure_ascii=False
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
        {
            "type": "tool_result",
            "tool_result": {"status": "error", "output": output},
        },
        ensure_ascii=False,
    )


def _event_tool_result_ok(output: str) -> str:
    """一条成功的工具回执。"""
    return json.dumps(
        {
            "type": "tool_result",
            "tool_result": {"status": "ok", "output": output},
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 演练主体
# ---------------------------------------------------------------------------
def main() -> int:
    """跑一遍「猎杀时刻」演练。"""
    parser = argparse.ArgumentParser(description="哨兵「猎杀时刻」压测")
    parser.add_argument(
        "--keep", action="store_true", help="保留现场目录（默认删掉）"
    )
    args = parser.parse_args()

    from src.adapters.mcode import McodeAdapter
    from src.core.engine import ContextTracker, SentinelEngine
    from src.core.guardian import Guardian
    from src.core.notifier import Notifier
    from src.core.safepoint import SafePointService
    from src.rules.basic import RuleEngine

    workdir = tempfile.mkdtemp(prefix="sentinel-kill-")
    testdir = os.path.join(workdir, "project")
    os.makedirs(os.path.join(testdir, "tests"), exist_ok=True)
    with open(os.path.join(testdir, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("def main():\n    return 'good'\n")
    with open(os.path.join(testdir, "junk.tmp"), "w", encoding="utf-8") as fh:
        fh.write("temp\n")
    with open(
        os.path.join(testdir, "tests", "test_kill.py"), "w", encoding="utf-8"
    ) as fh:
        fh.write("def test_always_fails():\n    assert False\n")

    log_path = os.path.join(workdir, "agent.jsonl")
    alert_log = os.path.join(workdir, "sentinel_alert.log")
    config_path = os.path.join(workdir, "config.yaml")

    hook = _mock_webhook(workdir)
    config = _base_config(workdir, hook["url"])
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)

    print("=" * 70)
    print("  哨兵 ·「猎杀时刻」压测（v2.0.0）")
    print("=" * 70)
    print(f"现场目录：{workdir}")
    print(f"假 Webhook：{hook['url']}")

    # --- 1) 启动「被监控进程」（一个可被挂起的 sleep）---
    agent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    print(f"被监控进程 PID={agent.pid}")

    config["guardian"]["target_pid"] = agent.pid
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)

    guardian = Guardian(config, target_pid=agent.pid)
    notifier = Notifier(config)
    safe_points = SafePointService(
        root=os.path.join(workdir, "safepoints"),
        max_seconds=3.0,
    )
    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config),
        alert_log_path=alert_log,
        console=False,
        llm_client=None,
        context_tracker=ContextTracker(window=5, stuck_threshold=3),
        intent_tracker=None,
        notifier=notifier,
        guardian=guardian,
        agent_name="kill-moment",
        safe_point_service=safe_points,
        workdir=testdir,
    )

    result = _Result()

    # --- 2) 流式喂事件：模拟一个彻底失控的 Agent ---
    def _feed() -> None:
        """把「猎杀时刻」的事件流写进日志。"""
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                _event_user("修好 tests/test_kill.py 这个失败的测试")
                + "\n"
            )
            fh.flush()
            time.sleep(0.6)

            for round_index in range(5):
                # 每轮三次失败 → 触发一次 [STUCK]。
                for attempt in range(3):
                    fh.write(
                        _event_thinking(
                            "再跑一次应该就好了。"
                        ) + "\n"
                    )
                    fh.write(_event_tool_call("Bash", STUCK_COMMAND) + "\n")
                    fh.write(
                        _event_tool_result_error(
                            "FAILED tests/test_kill.py::test_always_fails"
                        ) + "\n"
                    )
                    fh.flush()
                    time.sleep(0.35)

                # 第 2 轮之后 —— Agent 「顺手」清理临时文件。
                if round_index == 1:
                    # Agent 先把代码改坏（这是「改乱了」的那部分）。
                    with open(
                        os.path.join(testdir, "app.py"), "w", encoding="utf-8"
                    ) as fh2:
                        fh2.write("def main():\n    return 'broken'\n")
                    with open(
                        os.path.join(testdir, "scratch.py"), "w", encoding="utf-8"
                    ) as fh2:
                        fh2.write("# Agent 新建的垃圾文件\n")
                    fh.write(
                        _event_tool_call("Bash", DANGEROUS_COMMAND) + "\n"
                    )
                    fh.write(
                        _event_tool_result_ok("") + "\n"
                    )
                    fh.flush()
                    time.sleep(0.5)

            fh.write(
                _event_tool_result_ok("1 passed") + "\n"
            )
            fh.write(
                json.dumps(
                    {"type": "result", "result": {"status": "ok"}},
                    ensure_ascii=False,
                ) + "\n"
            )
            fh.flush()

    # 基线安全点要在「Agent 动手之前」打 ——
    # 所以先起引擎（tail 模式），再开始喂事件。
    def _run_engine() -> None:
        """后台跑引擎。"""
        try:
            engine.run(log_path, tail=True)
        except Exception as exc:  # noqa: BLE001 —— 演练里的失败要报出来
            print(f"引擎异常：{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=_run_engine, daemon=True)
    worker.start()
    time.sleep(1.5)  # 等基线安全点打好

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    feeder.join(timeout=60)
    time.sleep(3.0)  # 等最后一条告警落地

    # --- 3) 检查 ---
    print()
    print("---- 检查 ----")
    alerts = list(engine.alerts)
    verdicts = [str(item.get("verdict") or "") for item in alerts]
    stuck_indexes = [
        index for index, v in enumerate(verdicts) if v == "STUCK"
    ]
    dangerous_indexes = [
        index for index, v in enumerate(verdicts) if v == "DANGEROUS"
    ]

    result.check(
        "C1a", "触发 5 次 [STUCK]",
        len(stuck_indexes) == 5,
        f"实际 {len(stuck_indexes)} 次",
    )
    result.check(
        "C1b", "中间出现 1 次红线级 DANGEROUS",
        len(dangerous_indexes) == 1,
        f"实际 {len(dangerous_indexes)} 次",
    )
    result.check(
        "C1c", "STUCK 在前、DANGEROUS 在中间",
        bool(stuck_indexes) and bool(dangerous_indexes)
        and stuck_indexes[0] < dangerous_indexes[0] < stuck_indexes[-1],
        f"STUCK 位置={stuck_indexes}，DANGEROUS 位置={dangerous_indexes}",
    )

    # --- C2 进程真的被挂起了吗 ---
    suspended = False
    try:
        import psutil

        proc = psutil.Process(agent.pid)
        suspended = proc.status() == psutil.STATUS_STOPPED
    except Exception as exc:  # noqa: BLE001
        print(f"         （读进程状态失败：{exc}）")
    result.check(
        "C2", "红线级真的挂起了被监控进程",
        suspended,
        f"进程状态：{'已挂起' if suspended else '未挂起'}",
    )

    # --- C3 安全点 ---
    dangerous_alert = None
    for item in alerts:
        if str(item.get("verdict") or "") == "DANGEROUS":
            dangerous_alert = item
            break
    safe_point = (
        (dangerous_alert or {}).get("safe_point") or {}
        if dangerous_alert
        else {}
    )
    result.check(
        "C3", "挂起之前打了安全点快照",
        bool(safe_point.get("id")),
        "快照 ID={}，{} 个文件，{} 模式，耗时 {} 秒".format(
            safe_point.get("id"),
            safe_point.get("files_count"),
            safe_point.get("mode"),
            safe_point.get("elapsed"),
        ),
    )

    # --- C4 回滚 ---
    rollback = safe_points.restore("", testdir)
    with open(os.path.join(testdir, "app.py"), encoding="utf-8") as fh:
        restored = fh.read()
    new_file_gone = not os.path.exists(os.path.join(testdir, "scratch.py"))
    result.check(
        "C4", "一键回滚把改坏的代码退回去",
        "good" in restored and new_file_gone,
        "恢复 {} 个文件、删除 {} 个新增文件，耗时 {} 秒；"
        "app.py 现在是 {}；新增文件已删除={}".format(
            rollback.get("restored_count"),
            rollback.get("deleted_count"),
            rollback.get("elapsed"),
            "good" if "good" in restored else "**仍是坏的**",
            new_file_gone,
        ),
    )

    # --- C5 合并推送 ---
    time.sleep(1.5)
    payloads = list(hook["received"])
    result.check(
        "C5", "多次警告合并推送，没有骚扰",
        len(payloads) <= 3,
        "6 条告警只推了 {} 次（STUCK 合并成 1 次，"
        "红线级单独 1 次）".format(len(payloads)),
    )

    # --- C7 记录型通道一条不少 ---
    record_path = os.path.join(workdir, "alerts.jsonl")
    recorded = 0
    if os.path.exists(record_path):
        with open(record_path, encoding="utf-8") as fh:
            recorded = sum(1 for _ in fh)
    result.check(
        "C7", "记录型通道一条不少（流水账）",
        recorded == len(alerts),
        "文件通道记了 {} 条，引擎产出 {} 条".format(recorded, len(alerts)),
    )

    # --- C6 指令注入 ---
    from src.core.injector import AgentInjector, InboxStore

    inbox = InboxStore(os.path.join(testdir, "sentinel_inbox.json"))

    # (a) stdin 直写 —— 用一个真的读 stdin 的子进程来验。
    #
    # ⚠️ 子进程必须**显式**把 stdin 设成 UTF-8 ——
    # Windows 上 Python 默认按系统 ANSI 代码页（GBK）解码，
    # 哨兵写出去的是 UTF-8 字节，两边对不上就会读成乱码。
    # 真实的 CLI（node / Rust 写的 codex）默认就是 UTF-8。
    sink_path = os.path.join(workdir, "stdin_sink.txt")
    sink = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.stdin.reconfigure(encoding='utf-8')\n"
            "open(sys.argv[1], 'a', encoding='utf-8')"
            ".write(sys.stdin.readline())\n",
            sink_path,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    time.sleep(1.0)

    def _stdin_writer(text: str) -> bool:
        """往子进程的 stdin 写一行（写完必须 flush）。"""
        sink.stdin.write(str(text) + "\n")
        sink.stdin.flush()
        return True

    injector = AgentInjector(
        inbox=inbox,
        stdin_writer=_stdin_writer,
        agent_label="kill-moment",
    )
    out = injector.inject(
        "先复述你的任务目标，别再乱删文件了。", verdict="STUCK"
    )
    time.sleep(1.0)
    try:
        sink.stdin.close()
    except Exception as exc:  # noqa: BLE001
        print(
            "关闭子进程 stdin 失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )
    sink.wait(timeout=10)
    sink_err = sink.stderr.read() if sink.stderr else ""
    stdin_text = ""
    if os.path.exists(sink_path):
        with open(sink_path, encoding="utf-8") as fh:
            stdin_text = fh.read()
    result.check(
        "C6a", "stdin 直写：Agent 真的收到了指令",
        out.channel == "stdin" and "复述你的任务目标" in stdin_text,
        "渠道={}；Agent 收到 {} 字{}".format(
            out.channel, len(stdin_text), f"；子进程报错：{sink_err[:800]}"
            if sink_err else ""
        ),
    )

    # (b) 降级到留言箱。
    fallback = AgentInjector(inbox=inbox)
    out2 = fallback.inject("别再删文件了。", verdict="STUCK")
    inbox_path = os.path.join(testdir, "sentinel_inbox.json")
    pending = []
    if os.path.exists(inbox_path):
        with open(inbox_path, encoding="utf-8") as fh:
            data = json.load(fh)
        pending = [
            item for item in data.get("messages", [])
            if not item.get("delivered")
        ]
    result.check(
        "C6b", "CLI 不支持 stdin 时降级到留言箱",
        out2.channel == "inbox" and len(pending) >= 1,
        "留言箱={}，待投 {} 条，下一步指引 {} 字".format(
            inbox_path, len(pending), len(out2.next_step)
        ),
    )

    # (c) 重启流程注入上下文。
    rendered = inbox.render_for_prompt()
    result.check(
        "C6c", "重启流程能把留言注入任务提示",
        "别再删文件了" in rendered,
        "任务提示里带上了 {} 条留言".format(len(inbox.pending())),
    )

    # --- 收尾 ---
    print()
    print("---- 现场 ----")
    print(f"告警总数：{len(alerts)}（STUCK {len(stuck_indexes)}，"
          f"DANGEROUS {len(dangerous_indexes)}）")
    print(f"Webhook 实收：{len(payloads)} 次")
    print(f"安全点目录：{os.path.join(workdir, 'safepoints')}")

    engine.close()
    notifier.close()
    try:
        hook["stop"]()
        hook["close"]()
    except Exception as exc:  # noqa: BLE001
        print(
            "停止热重载监听失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )
    try:
        agent.terminate()
    except Exception as exc:  # noqa: BLE001
        print(
            "结束 Agent 进程失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )

    print()
    print("=" * 70)
    failed = result.failed
    if failed:
        print(f"  结果：{len(failed)} 项失败 / 共 {len(result.rows)} 项")
        for row in failed:
            print(f"    · {row['code']} {row['title']}")
        print("=" * 70)
        return 1
    print(f"  结果：{len(result.rows)} 项检查全部通过")
    print("=" * 70)

    if args.keep:
        print(f"现场保留在：{workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
