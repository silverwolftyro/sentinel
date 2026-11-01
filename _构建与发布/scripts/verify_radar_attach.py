"""雷达挂载的实机验证脚本（v2.3.0）。

跑一遍**完整链路**，每一步都断言：

1. 造一个「已经在跑的 Agent」的会话目录
   （``~/.codex/sessions/rollout.jsonl`` 那种），
   用环境变量 ``SENTINEL_RADAR_ROOTS`` 指给哨兵；
2. 起看板（**不带** ``--demo`` —— 这是真实监控模式）；
3. ``GET /api/discover`` —— 哨兵**发现**了那个日志；
4. ``POST /api/attach`` —— 把日志**挂载**进来，
   会话名自动变成「[Codex] <工作区>」；
5. 往那个日志里追加一条**高危命令**（模拟 Agent 正在干活）；
6. 轮询 ``/api/sessions/{id}/alerts`` —— 告警**真的出现了**。

⚠️ 为什么值得单独写这个脚本
=========================

任务三（会话识别）与任务一（挂载）单测都能过，
但用户真正在意的是那条**端到端的链**：
「我自己在终端里跑着 Agent → 哨兵发现它 → 我点一下接入 → 它出事了哨兵立刻知道」。

这段链里有**线程、文件描述符、tail -f 语义、SSE** ——
单元测试替身测不出来，只能实机跑。

用法::

    python _构建与发布/scripts/verify_radar_attach.py --port 8000
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

#: 一段「Agent 已经在跑」的 Codex 风格日志（前几行是正常的思考）。
AGENT_PREAMBLE = [
    {
        "type": "thread.started",
        "thread_id": "01a17af0-verify",
        "cwd": "D:\\work\\project_alpha",
    },
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "agent_message",
            "text": "先看看当前目录有哪些文件。",
        },
    },
]

#: Agent 干活时写下的一条**高危命令**（批量删除）。
DANGEROUS_EVENT = {
    "type": "item.completed",
    "item": {
        "id": "item_1",
        "type": "command_execution",
        "command": "find . -name '*.tmp' -delete",
        "aggregated_output": "",
        "exit_code": 0,
        "status": "completed",
    },
}


def _prepare_stdout() -> None:
    """控制台是 GBK 的话，打中文会炸 —— 先切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (ValueError, OSError, AttributeError):
            pass


def _fetch(url: str, timeout: float = 4.0):
    """抓一个 URL；失败返回 ``None``。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 还没起来是正常的
        return None


def _post(url: str, payload: dict, timeout: float = 10.0):
    """POST 一个 JSON；返回 ``(状态码, 解析后的 JSON)``。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def _append_event(path: str, payload: dict) -> None:
    """往日志尾部追加一行 JSON（模拟 Agent 正在写）。"""
    with io.open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


def _verify_packaged(exe: str, root: str, port: int, wait: float) -> int:
    """验证**打包版** exe 里的新资源都在。

    ⚠️ 为什么必须单独验
    ==================

    ``src/radar.py`` 是**函数内惰性导入**的 ——
    PyInstaller 的静态分析看不到它，
    漏了的话：打包成功、exe 能跑、但「发现/挂载」一点就崩。

    同样地，``settings.html`` / ``router.js`` / ``settings.js``
    是**随包资源**，漏了的话设置页直接 404。

    所以这一段专门对着**打包产物**打一遍。

    :param exe: exe 路径。
    :param root: 项目根。
    :param port: 端口。
    :param wait: 等多久算启动超时。
    :return: 进程退出码。
    """
    archive = os.path.join(root, "logs", "packaged_assets_output.txt")
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    handle = io.open(archive, "w", encoding="utf-8", newline="\n")
    # 打包版 exe 必须显式带 ``--demo`` 才会进演示模式 ——
    # 裸跑（无参数）才默认进演示，带了别的参数就会被当成「接管真实 Agent」。
    proc = subprocess.Popen(
        [exe, "--demo", "--no-browser", "--port", str(port)],
        cwd=root,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:{}".format(port)
    steps = []
    try:
        alive = False
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if _fetch(base + "/api/state") is not None:
                alive = True
                break
            time.sleep(0.5)
        steps.append(("打包版能启动", alive))
        if not alive:
            print("看板没起来，看存档：{}".format(archive))
            return 1

        discover = _fetch(base + "/api/discover")
        steps.append((
            "雷达模块已打包（/api/discover 200）",
            discover is not None,
        ))
        if discover:
            try:
                payload = json.loads(discover)
                steps.append(("雷达扫描有结果", payload.get("ok") is True))
            except ValueError:
                steps.append(("雷达扫描有结果", False))

        settings = _fetch(base + "/settings")
        steps.append((
            "设置页已打包（/settings 200）",
            settings is not None and "设置" in settings,
        ))
        for asset in ("router.js", "settings.js"):
            body = _fetch(base + "/static/" + asset)
            steps.append((
                "{} 已打包".format(asset),
                body is not None,
            ))
        config = _fetch(base + "/api/config")
        steps.append((
            "配置接口可用（/api/config 200）",
            config is not None,
        ))
    finally:
        _shutdown(proc, handle)

    print("")
    print("=" * 58)
    passed = 0
    for name, ok in steps:
        print("  {} {}".format("✅" if ok else "❌", name))
        if ok:
            passed += 1
    print("=" * 58)
    print("结果：{}/{} 项通过".format(passed, len(steps)))
    print("终端输出存档：{}".format(archive))
    return 0 if passed == len(steps) else 1


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="verify_radar_attach",
        description="v2.3.0 实机验证：雷达发现 → 挂载 → 实时告警",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--wait", type=float, default=40.0)
    parser.add_argument(
        "--out", default=os.path.join("logs", "radar_attach_output.txt")
    )
    parser.add_argument(
        "--exe",
        action="store_true",
        help="用**打包版** exe 验证（而不是源码运行）—— "
             "src.radar 是惰性导入的，不验这一遍就不知道它有没有被打进包",
    )
    args = parser.parse_args(argv)
    _prepare_stdout()

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    server = os.path.join(root, "src", "gui", "server.py")
    exe = os.path.join(root, "_构建与发布", "dist", "sentinel.exe")
    if args.exe:
        if not os.path.isfile(exe):
            print("找不到打包产物：{}".format(exe))
            return 2
    elif not os.path.isfile(server):
        print("找不到服务端源码：{}".format(server))
        return 2

    if args.exe:
        print("[1/1] 验证打包版 exe 里的新资源")
        return _verify_packaged(exe, root, args.port, args.wait)

    workdir = tempfile.mkdtemp(prefix="sentinel-radar-")
    # 假装这是某个 Agent 的本地会话目录。
    sessions_dir = os.path.join(workdir, ".codex", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    agent_log = os.path.join(sessions_dir, "rollout.jsonl")
    with io.open(agent_log, "w", encoding="utf-8", newline="\n") as handle:
        for event in AGENT_PREAMBLE:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    # 看板自己盯着一个**无关**的文件 —— 被监控的 Agent 是「挂载」进来的。
    dummy = os.path.join(workdir, "dummy.jsonl")
    with io.open(dummy, "w", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type":"thinking","thinking":"（无关文件）"}\n')

    env = dict(os.environ)
    env["SENTINEL_RADAR_ROOTS"] = os.path.join(workdir, ".codex")
    env["PYTHONIOENCODING"] = "utf-8"

    archive = os.path.join(root, args.out)
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    handle_out = io.open(archive, "w", encoding="utf-8", newline="\n")

    print("[1/6] 起看板（真实监控模式，不带 --demo）")
    if args.exe:
        command = [
            exe,
            "--watch",
            dummy,
            "--port",
            str(args.port),
            "--no-browser",
            "--no-health",
            "--log-level",
            "WARNING",
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "src.gui.server",
            "--watch",
            dummy,
            "--port",
            str(args.port),
            "--no-browser",
            "--no-health",
            "--log-level",
            "WARNING",
        ]
    proc = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=handle_out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    base = "http://127.0.0.1:{}".format(args.port)
    alive = False
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _fetch(base + "/api/state") is not None:
            alive = True
            break
        time.sleep(0.5)

    steps = []
    steps.append(("看板启动", alive))
    if not alive:
        _shutdown(proc, handle_out)
        print("看板没起来，看存档：{}".format(archive))
        return 1

    try:
        # --- 2. 发现 --------------------------------------------------
        print("[2/6] 雷达扫描 ~/.codex 风格目录")
        payload = json.loads(_fetch(base + "/api/discover") or "{}")
        found = [
            item
            for item in payload.get("sessions") or []
            if os.path.normcase(str(item.get("path") or ""))
            == os.path.normcase(agent_log)
        ]
        steps.append(("发现会话日志", len(found) == 1))
        if found:
            item = found[0]
            print("      发现：{}".format(item.get("label")))
            print("      识别：{} / 工作区 {}".format(
                item.get("agent"), item.get("cwd") or "（未知）"
            ))
            steps.append(("识别为 Codex", item.get("agent") == "codex"))
            steps.append((
                "提取到工作区",
                item.get("cwd") == "D:\\work\\project_alpha",
            ))
        else:
            steps.append(("识别为 Codex", False))
            steps.append(("提取到工作区", False))

        # --- 3. 挂载 --------------------------------------------------
        print("[3/6] 挂载这个日志")
        _status, body = _post(base + "/api/attach", {"path": agent_log})
        session = body.get("session") or {}
        steps.append(("挂载成功", bool(body.get("ok"))))
        steps.append((
            "会话名带 [Codex]",
            str(session.get("label") or "").startswith("[Codex]"),
        ))
        steps.append((
            "会话带工作区",
            session.get("workspace") == "D:\\work\\project_alpha",
        ))
        print("      会话：{}".format(session.get("label")))

        # --- 4. Agent 干活 ---------------------------------------------
        print("[4/6] 模拟 Agent 写了一条高危命令")
        _append_event(agent_log, DANGEROUS_EVENT)

        # --- 5. 等告警 ------------------------------------------------
        print("[5/6] 等告警出现")
        alerts = []
        session_id = str(session.get("id") or "")
        deadline = time.time() + 25
        while time.time() < deadline:
            raw = _fetch(
                base + "/api/sessions/{}/alerts".format(session_id)
            ) or "{}"
            try:
                alerts = (json.loads(raw) or {}).get("alerts") or []
            except (ValueError, AttributeError):
                alerts = []
            if alerts:
                break
            time.sleep(0.7)
        steps.append(("产生告警", bool(alerts)))
        red = [
            item
            for item in alerts
            if str(item.get("severity") or "") == "red_line"
        ]
        steps.append(("告警是红线级", bool(red)))
        if alerts:
            first = alerts[0]
            print("      告警：{} / {}".format(
                first.get("severity"), first.get("reason")
            ))

        # --- 6. 卸载 --------------------------------------------------
        print("[6/6] 卸载会话")
        request = urllib.request.Request(
            base + "/api/sessions/{}".format(session_id), method="DELETE"
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                detached = response.status == 200
        except Exception:  # noqa: BLE001
            detached = False
        steps.append(("卸载会话", detached))
    finally:
        _shutdown(proc, handle_out)
        shutil.rmtree(workdir, ignore_errors=True)

    print("")
    print("=" * 58)
    passed = 0
    for name, ok in steps:
        print("  {} {}".format("✅" if ok else "❌", name))
        if ok:
            passed += 1
    print("=" * 58)
    print("结果：{}/{} 项通过".format(passed, len(steps)))
    print("终端输出存档：{}".format(archive))
    return 0 if passed == len(steps) else 1


def _shutdown(proc, handle) -> None:
    """收掉看板进程（连同子进程）并关掉输出文件。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        proc.wait(timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(
            "结束验证用进程失败（PID {}）：{}: {}".format(
                proc.pid, type(exc).__name__, exc
            ),
            file=sys.stderr,
        )
    try:
        handle.close()
    except Exception as exc:  # noqa: BLE001
        print(
            "关闭日志句柄失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
