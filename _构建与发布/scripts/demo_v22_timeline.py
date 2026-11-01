"""v2.2.0 案发时间线演示 —— 起一个**有真实告警**的看板。

这个脚本造一个**完整且真实**的现场：

* 跑**真实的** :class:`~src.core.engine.SentinelEngine` ——
  规则层与上下文追踪器都真的跑一遍，告警是引擎真判出来的；
* 打**真实的**安全点 —— 所以时间线上的红线节点能关联到 Diff 清单；
* 写**真实的** ``EVENT_JSON`` 操作事件（开始目标 / 注入 / 复活 / 热重载）；
* 看板上的【时间线】能真的拉出这条线。

用法::

    python _构建与发布/scripts/demo_v22_timeline.py --port 8293

脚本会一直跑着（看板在前台），按 Ctrl+C 结束。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)))

from src.adapters.mcode import McodeAdapter  # noqa: E402
from src.core.engine import SentinelEngine  # noqa: E402
from src.core.safepoint import SafePointService  # noqa: E402
from src.rules.basic import RuleEngine  # noqa: E402

#: 演示用的任务目标。
TASK_GOAL = "重构 auth 模块并修复登录后偶发 401 的问题"

#: 会话 ID —— 归档晨报时用它占一节。
SESSION_ID = "v22-timeline"


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


def _load_launcher():
    """加载启动器脚本（它不在 ``src`` 包里，是独立脚本）。"""
    path = os.path.join(
        PROJECT_ROOT, "_构建与发布", "scripts", "run_with_sentinel.py"
    )
    spec = importlib.util.spec_from_file_location("sentinel_launcher_v22", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: 一段**真实形态**的 Agent 会话日志（mcode 的流式 JSONL）。
#:
#: 里面埋了四种典型情况：
#:
#: 1. 反复读同一个文件 → 疑似死循环（LOOP）；
#: 2. 连着跑失败的测试 → 上下文卡死（STUCK）；
#: 3. 一条红线命令 → 守护者拦截（red_line）；
#: 4. 改坏文件 → 安全点之后才有 Diff 可看。
#:
#: ⚠️ 每个分组前面的秒数是**真的等一会儿** ——
#: 时间线上最值钱的信息是「隔了多久」，
#: 那必须是真的流逝时间，不能是编出来的。
AGENT_SCRIPT: List[Any] = [
    # (等几秒, [事件…])
    (0.0, [{"type": "user_message", "content": TASK_GOAL}]),
    (3.0, [
        {"type": "thinking", "thinking": "先看看 auth 模块现在长什么样。"},
        {
            "type": "tool_call",
            "tool_call": {"name": "Read", "arguments": {"path": "src/auth.py"}},
        },
        {"type": "tool_result", "tool_result": {"status": "ok", "output": "# auth"}},
    ]),
    (6.0, [
        {"type": "thinking", "thinking": "再看看测试文件。"},
        {
            "type": "tool_call",
            "tool_call": {"name": "Read", "arguments": {"path": "tests/test_auth.py"}},
        },
        {"type": "tool_result", "tool_result": {"status": "ok", "output": "# test"}},
    ]),
    (5.0, [
        {"type": "thinking", "thinking": "测试好像挂了，重跑一次看看。"},
        {
            "type": "tool_call",
            "tool_call": {"name": "Bash", "arguments": {"command": "pytest tests/test_auth.py"}},
        },
        {
            "type": "tool_result",
            "tool_result": {"status": "error", "output": "FAILED test_login_401"},
        },
    ]),
    (7.0, [
        {"type": "thinking", "thinking": "还是失败，再跑一次。"},
        {
            "type": "tool_call",
            "tool_call": {"name": "Bash", "arguments": {"command": "pytest tests/test_auth.py"}},
        },
        {
            "type": "tool_result",
            "tool_result": {"status": "error", "output": "FAILED test_login_401"},
        },
    ]),
    (8.0, [
        {"type": "thinking", "thinking": "第三次了，清一下缓存再试。"},
        {
            "type": "tool_call",
            "tool_call": {"name": "Bash", "arguments": {"command": "pytest tests/test_auth.py -x"}},
        },
        {
            "type": "tool_result",
            "tool_result": {"status": "error", "output": "FAILED test_login_401"},
        },
    ]),
    (9.0, [
        {"type": "thinking", "thinking": "别管测试了，直接把旧文件清干净重写吧。"},
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Bash",
                "arguments": {"command": "rm -rf ./build && rm -rf ./dist"},
            },
        },
        {"type": "tool_result", "tool_result": {"status": "ok", "output": ""}},
    ]),
    (10.0, [
        {"type": "thinking", "thinking": "顺手把整盘也清一下，快一点。"},
        {
            "type": "tool_call",
            "tool_call": {"name": "Bash", "arguments": {"command": "mkfs.ext4 /dev/sda1"}},
        },
        {"type": "tool_result", "tool_result": {"status": "ok", "output": ""}},
    ]),
]


def _build_workdir(root: str) -> str:
    """造一个工作目录 —— 里面要有真文件，快照与 Diff 才有东西可看。

    ⚠️ 每次都**清空重来** —— 安全点是累积的，
    不清的话第二次跑会看到上一轮留下的快照节点，复盘就乱了。
    只删**自己这个演示目录**，不碰别的地方。

    :param root: 演示目录。
    :return: 工作目录绝对路径。
    """
    workdir = os.path.abspath(root)
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "src"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
    with open(
        os.path.join(workdir, "src", "auth.py"),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(
            "def login(user, password):\n"
            "    # 登录后偶发 401 —— 待修\n"
            "    return {'ok': True}\n"
        )
    with open(
        os.path.join(workdir, "tests", "test_auth.py"),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(
            "def test_login_401():\n"
            "    assert False, '偶发 401'\n"
        )
    with open(
        os.path.join(workdir, ".gitignore"),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write("build/\ndist/\n__pycache__/\n")
    return workdir


def _run_real_engine(
    log_path: str, alert_log: str, workdir: str, safe_points
) -> Dict[str, Any]:
    """跑**真实**的引擎，把真实告警写进告警日志。

    ⚠️ 会话日志是**边写边审**的，而且按真实节奏推进 ——
    所以告警的时间戳是**真实流逝**的时间，
    时间线上的「距上一步 8 秒」是真话，不是编的。

    :param log_path: Agent 会话日志路径。
    :param alert_log: 哨兵告警日志路径。
    :param workdir: 工作目录。
    :param safe_points: 安全点服务。
    :return: 引擎统计。
    """
    adapter = McodeAdapter()
    engine = SentinelEngine(
        adapter,
        RuleEngine({}),
        alert_log_path=alert_log,
        console=False,
        agent_name="mcode",
        safe_point_service=safe_points,
        workdir=workdir,
    )
    alerts: List[Dict[str, Any]] = []
    with open(log_path, "w", encoding="utf-8") as handle:
        for delay, events in AGENT_SCRIPT:
            if delay:
                time.sleep(delay)
            for item in events:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
            for item in events:
                for event in adapter.parse_payload(item):
                    produced = engine.process_event(event)
                    if produced:
                        alerts.append(produced)
    engine.close()
    return {
        "alerts": alerts,
        "counts": dict(engine.counts),
        "events": engine.processed_events,
    }, engine


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="demo_v22_timeline",
        description="v2.2.0 案发时间线演示（真实告警 + 真实安全点）",
    )
    parser.add_argument("--port", type=int, default=8293)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--workdir",
        default=os.path.join(PROJECT_ROOT, "logs", "v22_demo"),
        help="演示工作目录",
    )
    args = parser.parse_args(argv)

    workdir = _build_workdir(args.workdir)
    raw_log = os.path.join(workdir, "agent.jsonl")
    alert_log = os.path.join(workdir, "alerts.log")
    if os.path.exists(alert_log):
        os.remove(alert_log)

    launcher = _load_launcher()

    # --- 安全点：先打基线，再让红线拦截时打第二张 --------------------
    safe_points = SafePointService(
        root=os.path.join(workdir, ".sentinel", "safepoints")
    )
    baseline = safe_points.capture(workdir, label="基线安全点 · 会话开始")
    print("[DEMO] 基线安全点：{}".format(baseline.id))

    # --- 模拟 Agent 把文件改坏（这样 Diff 才有东西可看）-----------------
    with open(
        os.path.join(workdir, "src", "auth.py"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "def login(user, password):\n"
            "    # 改砸了：把 401 的判断删了，还顺手把 token 校验也删了\n"
            "    return {'ok': True}\n"
            "\n"
            "def logout(user):\n"
            "    pass\n"
        )
    with open(
        os.path.join(workdir, "src", "session.py"), "w", encoding="utf-8"
    ) as handle:
        handle.write("# 新增的半成品文件\n")

    # --- 时间线的第一个节点：开始目标 ---------------------------------
    launcher._write_event(
        alert_log,
        "session_start",
        "开始目标",
        TASK_GOAL,
        agent="mcode",
        task=TASK_GOAL,
    )

    # --- 跑真实引擎 ----------------------------------------------------
    summary, engine = _run_real_engine(
        raw_log, alert_log, workdir, safe_points
    )
    print(
        "[DEMO] 引擎产出：{} 条告警 · {}".format(
            len(summary.get("alerts") or []),
            summary.get("counts"),
        )
    )

    # --- 几条操作事件（真实写过日志的才算数）--------------------------
    # ⚠️ 真的等一会儿 —— 「拦截之后过多久才动手补救」也是时间线的一部分。
    time.sleep(5.0)
    launcher._write_event(
        alert_log,
        "inject",
        "注入纠偏提示",
        "通道：inbox —— 提示已落进留言箱",
        rule_id="dangerous_command",
    )
    launcher._write_event(
        alert_log,
        "resurrect",
        "复活重试",
        "已回滚至安全点，并注入纠偏提示，重新唤醒 Agent。",
        severity="warn",
        point_id=baseline.id,
    )
    launcher._write_event(
        alert_log,
        "hot_reload",
        "规则热重载完成",
        "生效规则 {} 条".format(20),
    )

    # --- 起看板 --------------------------------------------------------
    import uvicorn

    from src.gui.server import SessionSpec, WatchTarget, create_app

    # --- 归档晨报（真实引擎产出，不是编的）---------------------------
    try:
        from src.core.briefing import (
            collect_from_engine,
            render_markdown,
            render_terminal,
            save_briefing,
        )

        stats = collect_from_engine(
            engine,
            session_id=SESSION_ID,
            agent="mcode",
        )
        markdown = render_markdown(
            stats,
            title="会话 {} · v22-demo".format(SESSION_ID),
            heading_level=2,
        )
        # ⚠️ **必须**传 session_id —— 不传就是「单路模式」，
        # 会把当天的日报**整个覆盖掉**（v1.2.0 那两节就没了）。
        path = save_briefing(
            markdown, archive_dir=None, session_id=SESSION_ID
        )
        print("[DEMO] 晨报已归档：{}".format(path))
        print(render_terminal(stats))
    except Exception as exc:  # noqa: BLE001 —— 归档失败不挡住演示
        print("[DEMO] 晨报归档跳过：{}: {}".format(type(exc).__name__, exc))

    spec = SessionSpec(
        id="v22-demo",
        label="案发时间线演示",
        agent="mcode",
        watch_path=raw_log,
        alert_log_path=alert_log,
        watch_target=WatchTarget(agent="mcode", goal=TASK_GOAL),
        safe_point_service=safe_points,
        workdir=workdir,
    )
    app = create_app(
        sessions=[spec],
        host=args.host,
        port=args.port,
    )
    print(
        "[DEMO] 看板已就绪：http://{}:{}/ —— 点顶栏【时间线】看复盘".format(
            args.host, args.port
        )
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
