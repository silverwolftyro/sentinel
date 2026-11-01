"""v3.0.0 的实机验收：自动提取意图 + 武装推荐 + 移动端窄屏。

这个脚本把整条演示**自动化**了：

1. 造一个**已经在跑的**假 Codex Agent：

   * 日志的**第一行**就是 ``user_message``（用户最初那句 Prompt）
     —— 这样哨兵才**自动提取**得到目标；
   * 命令行里**带着**它写的日志路径
     —— 这样哨兵才**反查得到** PID（置信度 0.95）。

2. 起一个**真实监控模式**的看板（不带 ``--demo``）；
3. 通过 ``/api/attach`` 挂载这份日志；
4. 断言三件事：

   * ``goal_source == "auto"`` —— 目标**自动提取**到了；
   * ``task_goal`` 内容 = 日志里那句 Prompt；
   * ``process_bound`` 且置信度 ≥ 0.95 —— 武装**可建议**；

5. 截图 ①：桌面 —— 侧栏的 ``[自动提取]`` 徽标 +「建议武装」高亮 + 气泡；
6. 截图 ②：手机 390×844 —— 看板；
7. 截图 ③：手机 390×844 —— 设置页；
8. 截图 ④：手机 390×844 —— 侧栏（武装建议那一块）。

⚠️ 本脚本**绝不**改动用户自己的 ``config/config.yaml`` ——
所有配置都写进临时目录。

用法::

    python _构建与发布/scripts/capture_v300_pages.py --port 8000
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

#: 假 Agent 最初那句 Prompt —— 哨兵要把它**自动提取**成任务目标。
AUTO_GOAL = "重构 auth 模块，把登录鉴权改成 JWT，并补上单元测试"

#: 手机视口（iPhone 12/13/14 逻辑分辨率）。
MOBILE_WIDTH = 390
MOBILE_HEIGHT = 844


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
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
        except (ValueError, OSError, AttributeError) as exc:
            print(
                "切换控制台编码失败：{}: {}".format(
                    type(exc).__name__, exc
                ),
                file=sys.stderr,
            )


def _fetch(url: str, timeout: float = 4.0):
    """抓一个 URL；失败返回 ``None``。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8", "replace")
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def _post(url: str, payload, timeout: float = 8.0):
    """POST 一个 JSON；失败返回 ``{}``。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        print(
            "POST {} 失败：{}: {}".format(url, type(exc).__name__, exc),
            file=sys.stderr,
        )
        return {}


def _write_report(path: str, report) -> None:
    """落盘验收报告。"""
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _capture(
    capture: str,
    base: str,
    out: str,
    width: int,
    height: int,
    wait: float,
    click: str = "",
    eval_js: str = "",
) -> bool:
    """调截图脚本；成功返回 ``True``。"""
    argv = [
        sys.executable,
        capture,
        "--url",
        base,
        "--out",
        out,
        "--width",
        str(width),
        "--height",
        str(height),
        "--wait",
        str(wait),
    ]
    if click:
        argv += ["--click", click]
    if eval_js:
        argv += ["--eval", eval_js]
    result = subprocess.run(
        argv,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        ))),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if result.returncode != 0:
        print(
            "截图失败（{}）：{}".format(out, result.stdout + result.stderr),
            file=sys.stderr,
        )
        return False
    print("      → {}".format(out))
    return True


def _shutdown(proc, handle_out) -> None:
    """把子进程连同它的**整棵进程树**收掉。"""
    try:
        if proc is not None and proc.poll() is None:
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as exc:  # noqa: BLE001
        print(
            "结束看板进程失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )
    try:
        if handle_out is not None:
            handle_out.close()
    except Exception as exc:  # noqa: BLE001
        print(
            "关闭输出句柄失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# 假 Agent 进程
# ---------------------------------------------------------------------------
def _fake_agent_main(argv) -> int:
    """假 Codex Agent：**第一行就写 user_message**，然后一直活着。

    ⚠️ 第一行必须是 ``user_message`` —— 哨兵只扫日志**开头** 200 行，
    首条用户消息写在最前面才「自动提取得到」。
    """
    parser = argparse.ArgumentParser(prog="fake-agent")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--log", required=True)
    parser.add_argument("--seconds", type=float, default=900.0)
    args = parser.parse_args(argv)

    path = args.log
    os.makedirs(os.path.dirname(path), exist_ok=True)
    events = [
        # ↓↓↓ 首条用户消息 —— 自动锚定的锚点
        {
            "type": "user_message",
            "content": AUTO_GOAL,
        },
        {
            "type": "thread.started",
            "thread_id": "fake-{}".format(os.getpid()),
            "cwd": args.cwd,
        },
        {"type": "turn.started"},
    ]
    with io.open(path, "a", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()

    manifest = os.environ.get("FAKE_AGENT_MANIFEST", "")
    if manifest:
        try:
            with io.open(manifest, "a", encoding="utf-8", newline="\n") as out:
                out.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "cwd": args.cwd,
                            "path": path,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                out.flush()
        except OSError as exc:
            print(
                "写 Agent 清单失败：{}: {}".format(type(exc).__name__, exc),
                file=sys.stderr,
            )

    deadline = time.time() + args.seconds
    while time.time() < deadline:
        time.sleep(0.5)
    return 0


# ---------------------------------------------------------------------------
# 临时配置
# ---------------------------------------------------------------------------
def _make_temp_config(root: str, workdir: str) -> str:
    """造一份**临时**配置（绝不改用户自己的 ``config/config.yaml``）。"""
    sys.path.insert(0, root)
    from src.config_loader import load_config  # noqa: PLC0415

    config = load_config(os.path.join(root, "config", "config.yaml"))
    # 本次只验证「自动提取 + 武装建议 + 移动端」，
    # 不需要模型层 —— 关掉它，省一次网络往返。
    llm = dict(config.get("llm") or {})
    llm["enabled"] = False
    config["llm"] = llm
    config["task_goal"] = ""

    import yaml  # noqa: PLC0415

    path = os.path.join(workdir, "config.v300.yaml")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(
            config, handle, allow_unicode=True, sort_keys=False
        )
    return path


# ---------------------------------------------------------------------------
# 窄屏溢出探针
# ---------------------------------------------------------------------------
#: 量「有没有元素比视口还宽」。返回 JSON 字符串。
WIDTH_PROBE_JS = (
    "(function(){"
    "var vw=document.documentElement.clientWidth;"
    "var out=[];"
    "document.querySelectorAll('*').forEach(function(el){"
    "var r=el.getBoundingClientRect();"
    "if(r.width>vw+1){"
    "out.push(el.tagName+'.'+String(el.className||'').slice(0,30)"
    "+' w='+Math.round(r.width));"
    "}"
    "});"
    "return JSON.stringify({vw:vw,"
    "scrollW:document.documentElement.scrollWidth,wide:out.slice(0,10)});"
    "})()"
)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    """跑完整条验收。"""
    # ⚠️ 「假 Agent 模式」必须在**主参数解析之前**分流 ——
    # 它的参数（``--cwd`` / ``--log``）主 parser 根本不认，
    # 先 parse 就会报「unrecognized arguments」。
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--fake-agent" in raw:
        _prepare_stdout()
        fake_argv = [a for a in raw if a != "--fake-agent"]
        raise SystemExit(_fake_agent_main(fake_argv))

    parser = argparse.ArgumentParser(
        description="v3.0.0 验收：自动提取意图 + 武装推荐 + 移动端窄屏"
    )
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--wait", type=float, default=30.0)
    args = parser.parse_args(raw)
    _prepare_stdout()
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    assets = os.path.join(root, "_管理文档", "文档与报告", "assets")
    os.makedirs(assets, exist_ok=True)
    auto_png = os.path.join(assets, "dashboard-v3.0.0-auto-anchor.png")
    mobile_png = os.path.join(assets, "dashboard-v3.0.0-mobile.png")
    mobile_side_png = os.path.join(
        assets, "dashboard-v3.0.0-mobile-arm-suggest.png"
    )
    mobile_settings_png = os.path.join(
        assets, "dashboard-v3.0.0-mobile-settings.png"
    )
    report_json = os.path.join(
        root, "_管理文档", "文档与报告", "v3.0.0-实机验证.json"
    )

    report = {
        "version": "v3.0.0",
        "port": args.port,
        "auto_goal": AUTO_GOAL,
        "attach": {},
        "shots": {},
        "errors": [],
    }

    workdir = tempfile.mkdtemp(prefix="sentinel-v300-")
    manifest = os.path.join(workdir, "agents.jsonl")
    codex_dir = os.path.join(workdir, ".codex", "sessions")
    os.makedirs(codex_dir, exist_ok=True)

    env = dict(os.environ)
    env["FAKE_AGENT_MANIFEST"] = manifest
    env["PYTHONIOENCODING"] = "utf-8"
    env["SENTINEL_RADAR_ROOTS"] = os.path.join(workdir, ".codex")

    script = os.path.abspath(__file__)
    codex_log = os.path.join(codex_dir, "rollout-v300.jsonl")

    print("[1/5] 造一个「已经在跑的 Agent」（首行是 user_message）")
    agent_proc = subprocess.Popen(
        [
            sys.executable,
            script,
            "--fake-agent",
            "--cwd",
            "D:\\work\\project_alpha",
            "--log",
            codex_log,
        ],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(2.0)

    manifest_rows = []
    if os.path.isfile(manifest):
        with io.open(manifest, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line:
                    manifest_rows.append(json.loads(line))
    if not manifest_rows:
        print("假 Agent 没起来 —— 清单：{}".format(manifest))
        report["errors"].append("假 Agent 没起来")
        _write_report(report_json, report)
        return 1
    report["agents"] = manifest_rows
    agent_pid = manifest_rows[0].get("pid")
    # --- 看板 ---------------------------------------------------------
    primary_log = os.path.join(workdir, "primary.jsonl")
    with io.open(primary_log, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("")
    alert_log = os.path.join(workdir, "alerts.log")
    with io.open(alert_log, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("")

    try:
        config_path = _make_temp_config(root, workdir)
    except Exception as exc:  # noqa: BLE001
        print("造临时配置失败：{}".format(exc))
        report["errors"].append("造临时配置失败：{}".format(exc))
        _write_report(report_json, report)
        return 1

    archive = os.path.join(root, "logs", "v300_shot_output.txt")
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    handle_out = io.open(archive, "w", encoding="utf-8", newline="\n")

    print("[2/5] 起看板（真实监控模式）")
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
            "--config",
            config_path,
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
        print("看板没起来，看存档：{}".format(archive))
        report["errors"].append("看板没起来")
        _shutdown(proc, handle_out)
        agent_proc.terminate()
        _write_report(report_json, report)
        return 1

    try:
        print("[3/5] 挂载 → 验证自动提取 + 武装建议")
        body = _post(base + "/api/attach", {"path": codex_log})
        snapshot = body.get("session") or {}
        session_id = snapshot.get("id", "")
        info = snapshot.get("process_info") or {}
        report["attach"] = {
            "label": snapshot.get("label"),
            "expected_pid": agent_pid,
            "goal": snapshot.get("goal"),
            "goal_source": snapshot.get("goal_source"),
            "goal_source_label": snapshot.get("goal_source_label"),
            "auto_anchored": snapshot.get("auto_anchored"),
            "pid": snapshot.get("pid"),
            "process_bound": snapshot.get("process_bound"),
            "arm_available": snapshot.get("arm_available"),
            "armed": snapshot.get("armed"),
            "strategy": info.get("strategy"),
            "confidence": info.get("confidence"),
            "message": body.get("message"),
        }
        print("      标签      → {}".format(snapshot.get("label")))
        print("      目标      → {}".format(snapshot.get("goal")))
        print("      目标来源  → {}（{}）".format(
            snapshot.get("goal_source"),
            snapshot.get("goal_source_label"),
        ))
        print("      PID       → {}（{}，置信度 {}）".format(
            snapshot.get("pid"),
            info.get("strategy"),
            info.get("confidence"),
        ))
        print("      可武装    → {}".format(snapshot.get("arm_available")))

        if snapshot.get("goal_source") != "auto":
            report["errors"].append("目标来源不是 auto")
        if AUTO_GOAL not in str(snapshot.get("goal") or ""):
            report["errors"].append(
                "自动提取的目标内容不对：{}".format(snapshot.get("goal"))
            )
        if not snapshot.get("process_bound"):
            report["errors"].append("没绑定到进程")
        confidence = float(info.get("confidence") or 0)
        if confidence < 0.95:
            report["errors"].append(
                "置信度不够武装建议：{}".format(confidence)
            )

        print("[4/5] 截图")
        capture = os.path.join(
            root, "_构建与发布", "scripts", "capture_dashboard.py"
        )
        selector = ".session-item[data-session-id='{}']".format(session_id)

        report["shots"]["desktop_auto_anchor"] = _capture(
            capture, base, auto_png, args.width, args.height, 8.0,
            click=selector,
        )
        report["shots"]["mobile_dashboard"] = _capture(
            capture, base, mobile_png, MOBILE_WIDTH, MOBILE_HEIGHT, 8.0,
        )
        report["shots"]["mobile_arm_suggest"] = _capture(
            capture, base, mobile_side_png, MOBILE_WIDTH, MOBILE_HEIGHT, 8.0,
            click=selector,
        )
        report["shots"]["mobile_settings"] = _capture(
            capture,
            base + "/settings",
            mobile_settings_png,
            MOBILE_WIDTH,
            MOBILE_HEIGHT,
            8.0,
        )

        # --- 窄屏溢出探针（两个页面各量一次）---
        print("[5/5] 量窄屏溢出")
        for label, url in (
            ("dashboard", base + "/"),
            ("settings", base + "/settings"),
        ):
            probe_out = os.path.join(
                workdir, "probe-{}.txt".format(label)
            )
            probe = subprocess.run(
                [
                    sys.executable,
                    capture,
                    "--url",
                    url,
                    "--out",
                    os.path.join(workdir, "probe-{}.png".format(label)),
                    "--width",
                    str(MOBILE_WIDTH),
                    "--height",
                    str(MOBILE_HEIGHT),
                    "--wait",
                    "3",
                    "--eval",
                    WIDTH_PROBE_JS,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            result = probe.stdout.strip()
            report.setdefault("width_probe", {})[label] = result
            print("      {:10s} → {}".format(label, result))
            del probe_out
    finally:
        _shutdown(proc, handle_out)
        try:
            agent_proc.terminate()
            agent_proc.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001
            print(
                "结束假 Agent 失败：{}: {}".format(
                    type(exc).__name__, exc
                ),
                file=sys.stderr,
            )
        shutil.rmtree(workdir, ignore_errors=True)

    _write_report(report_json, report)
    print()
    if report["errors"]:
        print("==== 验收未通过 ====")
        for item in report["errors"]:
            print("  ✗ {}".format(item))
        return 1
    print("==== v3.0.0 验收全部通过 ====")
    print("  报告：{}".format(report_json))
    print("  截图：{}".format(auto_png))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
