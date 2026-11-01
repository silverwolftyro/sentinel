"""v2.3.0 的两张验收截图：主看板（雷达挂载）+ 独立设置页。

这个脚本把整条演示**自动化**了：

1. 起一个**真实监控模式**的看板（不带 ``--demo``）；
2. 造两个「已经在跑的 Agent」会话（一个 Codex、一个 Claude），
   用环境变量 ``SENTINEL_RADAR_ROOTS`` 指给哨兵；
3. 通过 ``/api/attach`` 把它们挂载进来 ——
   看板左侧于是出现「[Codex] D:\\work\\project_alpha」这种名字；
4. 让其中一个 Agent 写一条**高危命令**，看板上出现红线卡片；
5. 截主看板 → ``assets/dashboard-v2.3.0-radar.png``；
6. 截独立设置页 → ``assets/settings-v2.3.0.png``。

用法::

    python _构建与发布/scripts/capture_v230_pages.py --port 8000
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
import urllib.request

#: 两个假 Agent 的会话（不同类型、不同工作区）。
AGENTS = (
    {
        "dir": ".codex",
        "file": "rollout.jsonl",
        "cwd": "D:\\work\\project_alpha",
        "preamble": [
            {
                "type": "thread.started",
                "thread_id": "01a17af0-alpha",
                "cwd": "D:\\work\\project_alpha",
            },
            {"type": "turn.started"},
        ],
        "dangerous": {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "find . -name '*.tmp' -delete",
                "exit_code": 0,
                "status": "completed",
            },
        },
    },
    {
        "dir": ".claude",
        "file": "session.jsonl",
        "cwd": "D:\\work\\project_beta",
        "preamble": [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "beta-0001",
                "cwd": "D:\\work\\project_beta",
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "先看看项目结构。"}
                    ]
                },
            },
        ],
        "dangerous": None,
    },
)


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
    except Exception:  # noqa: BLE001
        return None


def _post(url: str, payload: dict, timeout: float = 15.0):
    """POST 一个 JSON；返回解析后的字典。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _append_event(path: str, payload: dict) -> None:
    """往日志尾部追加一行 JSON（模拟 Agent 正在写）。"""
    with io.open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="capture_v230_pages",
        description="v2.3.0 验收截图：主看板（雷达挂载）+ 独立设置页",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--wait", type=float, default=40.0)
    parser.add_argument("--width", type=int, default=1700)
    parser.add_argument("--height", type=int, default=1050)
    args = parser.parse_args(argv)
    _prepare_stdout()

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    assets = os.path.join(root, "_管理文档", "文档与报告", "assets")
    os.makedirs(assets, exist_ok=True)
    dashboard_png = os.path.join(
        assets, "dashboard-v2.3.0-radar.png"
    )
    settings_png = os.path.join(assets, "settings-v2.3.0.png")

    workdir = tempfile.mkdtemp(prefix="sentinel-shot-")
    logs = []
    for spec in AGENTS:
        folder = os.path.join(workdir, spec["dir"])
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, spec["file"])
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            for event in spec["preamble"]:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        logs.append((spec, path))

    dummy = os.path.join(workdir, "dummy.jsonl")
    with io.open(dummy, "w", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type":"thinking","thinking":"（无关文件）"}\n')
    # ⚠️ 告警日志要用**干净的** ——
    # 默认那个 logs/sentinel_alert.log 里攒着历次演示的旧告警，
    # 看板一预热就会把它们全倒出来（截图上会是一屏历史噪音）。
    alert_log = os.path.join(workdir, "alerts.log")
    with io.open(alert_log, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("")

    env = dict(os.environ)
    env["SENTINEL_RADAR_ROOTS"] = ";;".join(
        os.path.join(workdir, spec["dir"]) for spec in AGENTS
    )
    env["PYTHONIOENCODING"] = "utf-8"

    # ⚠️ 主会话盯的是**第一路** Agent 的日志（Codex），而且开 ``--tail`` ——
    # 只有实时监听才能在 Agent 干活的那一刻就报出红线。
    # 第二路（Claude）走「发现 → 挂载」接进来，
    # 于是左侧会话列表里能同时看到两路 Agent 与它们的工作区。
    primary_log = logs[0][1]

    archive = os.path.join(root, "logs", "v230_shot_output.txt")
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    handle_out = io.open(archive, "w", encoding="utf-8", newline="\n")

    print("[1/5] 起看板")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "src.gui.server",
            "--watch",
            primary_log,
            "--tail",
            "--alert-log",
            alert_log,
            "--port",
            str(args.port),
            "--no-browser",
            "--no-health",
            "--log-level",
            "WARNING",
        ],
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
    if not alive:
        _shutdown(proc, handle_out)
        print("看板没起来，看存档：{}".format(archive))
        return 1

    try:
        print("[2/5] 挂载第二路 Agent（发现 → 接入）")
        for spec, path in logs[1:]:
            body = _post(base + "/api/attach", {"path": path})
            print("      {} → {}".format(
                spec["cwd"], body.get("session", {}).get("label")
            ))

        print("[3/5] 让 Codex 写一条高危命令")
        for spec, path in logs:
            if spec.get("dangerous"):
                _append_event(path, spec["dangerous"])
        time.sleep(6)

        capture = os.path.join(
            root, "_构建与发布", "scripts", "capture_dashboard.py"
        )
        print("[4/5] 截主看板")
        code = subprocess.call(
            [
                sys.executable,
                capture,
                "--url",
                base + "/",
                "--out",
                dashboard_png,
                "--width",
                str(args.width),
                "--height",
                str(args.height),
                "--wait",
                "8",
                "--probe",
            ],
            cwd=root,
        )
        print("[5/5] 截独立设置页")
        # 设置页比看板长 —— 用更高的视口，让四个分区**都**进画面。
        code2 = subprocess.call(
            [
                sys.executable,
                capture,
                "--url",
                base + "/settings",
                "--out",
                settings_png,
                "--width",
                str(args.width),
                "--height",
                str(args.height + 1600),
                "--wait",
                "8",
                "--probe",
            ],
            cwd=root,
        )
    finally:
        _shutdown(proc, handle_out)
        shutil.rmtree(workdir, ignore_errors=True)

    ok = code == 0 and code2 == 0
    print("")
    print("主看板　：{}".format(dashboard_png))
    print("设置页　：{}".format(settings_png))
    print("结果：{}".format("成功" if ok else "失败"))
    return 0 if ok else 1


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
            "结束浏览器进程失败（PID {}）：{}: {}".format(
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
