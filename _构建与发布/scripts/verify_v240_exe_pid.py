"""v2.4.0 打包版验证：**打包后的 exe 能不能真的反查到进程 PID**。

为什么必须单独验
================

打包成 exe 之后有两件事会变，而它们**直接影响**反查：

1. **进程名变了** —— 源码运行时是 ``python.exe``，
   打包后是 ``sentinel.exe``；
   反查器如果不做保护，会**把自己**当成被监控对象，
   武装守护者时**把自己挂起**；
2. **模块可能被漏掉** —— ``psutil`` 与 ``src.procscan`` 都是
   **函数内惰性导入**的，PyInstaller 的静态分析看不到 ——
   漏掉的代价是「打包成功，但反查永远失败」。

所以这个脚本**专门**用打包版跑一遍完整链路：

* 造一个「已经在跑的 Agent」（命令行里带着它写的日志路径）；
* 启动 ``sentinel.exe``（**打包版**，不是源码）；
* 通过 ``/api/attach`` 挂载那份日志；
* 断言：反查到的 PID **就是**那个 Agent 的 PID，
  而且**不是**哨兵自己的 PID；
* 再挂一份「路径不在命令行里」的日志 ——
  断言它显示「[未绑定进程]」（不装懂）；
* 最后真的点一次【武装守护者】——
  断言打包版的守护者**接得上**（``psutil`` 在包里）。

用法::

    python _构建与发布/scripts/verify_v240_exe_pid.py --port 8010
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

#: 打包产物路径（相对项目根）。
EXE_RELATIVE = os.path.join("_构建与发布", "dist", "sentinel.exe")

#: 假 Agent 脚本（复用验收脚本里的「假 Agent 进程」模式）。
FAKE_AGENT_RELATIVE = os.path.join(
    "_构建与发布", "scripts", "capture_v240_pages.py"
)


def _prepare_stdout() -> None:
    """控制台可能是 GBK —— 先切 UTF-8，否则中文输出会炸。"""
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


def _post(url: str, payload: dict, timeout: float = 30.0):
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


def _kill_tree(pid: int) -> None:
    """把进程连同**整棵进程树**收掉。

    ⚠️ 打包版是单文件模式，会再起一个同名子进程 ——
    只杀启动器的话，子进程会活下来继续占端口。
    """
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            "taskkill 结束进程树失败（PID {}）：{}: {}".format(
                pid, type(exc).__name__, exc
            ),
            file=sys.stderr,
        )


def _make_temp_config(root: str, workdir: str) -> str:
    """造一份**临时**配置（不改用户自己的 ``config/config.yaml``）。"""
    sys.path.insert(0, root)
    from src.config_loader import load_config  # noqa: PLC0415

    config = load_config(os.path.join(root, "config", "config.yaml"))
    llm = dict(config.get("llm") or {})
    llm["provider"] = "openai_compatible"
    llm["base_url"] = "https://api.deepseek.com"
    llm["model"] = "deepseek-chat"
    llm["api_key"] = ""
    llm.pop("api_key_env", None)
    config["llm"] = llm
    config["task_goal"] = ""

    import yaml  # noqa: PLC0415

    path = os.path.join(workdir, "config.v240.exe.yaml")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(
            config, handle, allow_unicode=True, sort_keys=False
        )
    return path


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="verify_v240_exe_pid",
        description="v2.4.0 打包版验证：exe 能不能真的反查到进程 PID",
    )
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--wait", type=float, default=90.0)
    args = parser.parse_args(argv)
    _prepare_stdout()

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    exe = os.path.join(root, EXE_RELATIVE)
    if not os.path.isfile(exe):
        print("找不到打包产物：{}".format(exe))
        return 1

    workdir = tempfile.mkdtemp(prefix="sentinel-v240-exe-")
    manifest = os.path.join(workdir, "agents.jsonl")
    codex_dir = os.path.join(workdir, ".codex", "sessions")
    claude_dir = os.path.join(workdir, ".claude", "projects", "beta")
    os.makedirs(codex_dir, exist_ok=True)
    os.makedirs(claude_dir, exist_ok=True)

    env = dict(os.environ)
    env["FAKE_AGENT_MANIFEST"] = manifest
    env["PYTHONIOENCODING"] = "utf-8"

    fake_agent = os.path.join(root, FAKE_AGENT_RELATIVE)
    checks = []
    ok = True

    def _check(name: str, passed: bool, detail: str = "") -> None:
        """记一条断言结果。"""
        nonlocal ok
        checks.append(
            {"name": name, "passed": bool(passed), "detail": detail}
        )
        if not passed:
            ok = False
        print("  [{}] {}{}".format(
            "PASS" if passed else "FAIL",
            name,
            " —— " + detail if detail else "",
        ))

    agents = []
    exe_proc = None
    try:
        print("[1/4] 造两个「已经在跑的 Agent」")
        bound_log = os.path.join(codex_dir, "rollout-bound.jsonl")
        agents.append(subprocess.Popen(
            [
                sys.executable, fake_agent,
                "--fake-agent",
                "--name", "codex",
                "--cwd", "D:\\work\\project_alpha",
                "--log", bound_log,
            ],
            cwd=root, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        ))
        agents.append(subprocess.Popen(
            [
                sys.executable, fake_agent,
                "--fake-agent",
                "--name", "claude",
                "--cwd", "D:\\work\\project_beta",
                "--log-dir", claude_dir,
            ],
            cwd=root, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        ))
        time.sleep(2.5)

        rows = []
        if os.path.isfile(manifest):
            with io.open(manifest, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if line:
                        rows.append(json.loads(line))
        bound_path = ""
        unbound_path = ""
        bound_pid = 0
        for row in rows:
            if row.get("cmdline_has_path"):
                bound_path = row.get("path", "")
                bound_pid = int(row.get("pid") or 0)
            else:
                unbound_path = row.get("path", "")
        if not bound_path or not unbound_path:
            print("假 Agent 没起来 —— 清单：{}".format(manifest))
            return 1

        print("[2/4] 起**打包版** exe")
        primary = os.path.join(workdir, "primary.jsonl")
        with io.open(primary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("")
        alerts = os.path.join(workdir, "alerts.log")
        with io.open(alerts, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("")
        config_path = _make_temp_config(root, workdir)

        # ⚠️ 打包版 exe **必须**显式带 ``--demo`` 才会进看板模式 ——
        # 裸跑（无参数）才默认进演示，带了别的参数就会被当成
        # 「接管真实 Agent」，进而去找一个根本不存在的 Agent 命令。
        # 演示模式的看板**一样**带全套雷达挂载能力（`attach_factory` 照常装配），
        # 所以用它验反查完全有效。
        proc = subprocess.Popen(
            [
                exe,
                "--demo",
                "--no-browser",
                "--no-health",
                "--config", config_path,
                "--port", str(args.port),
            ],
            cwd=root, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        exe_proc = proc
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
        _check("打包版 exe 能起来并对外服务", alive)
        if not alive:
            return 1

        print("[3/4] 挂载并验反查结果")
        body = _post(base + "/api/attach", {"path": bound_path})
        snapshot = (body or {}).get("session") or {}
        pid = int(snapshot.get("pid") or 0)
        _check(
            "反查到正确的 PID（命令行带路径）",
            pid == bound_pid and bound_pid > 0,
            "期望 {}，实得 {}".format(bound_pid, pid),
        )
        _check(
            "反查结果标记为「已绑定」",
            bool(snapshot.get("process_bound")),
            "strategy={}".format(
                (snapshot.get("process_info") or {}).get("strategy")
            ),
        )
        _check(
            "哨兵**没有**把自己当成被监控进程",
            pid != proc.pid,
            "exe 启动器 PID={}，反查 PID={}".format(proc.pid, pid),
        )
        _check(
            "武装开关可用",
            bool(snapshot.get("arm_available")),
        )

        body = _post(base + "/api/attach", {"path": unbound_path})
        other = (body or {}).get("session") or {}
        _check(
            "路径不在命令行里 → 显示「[未绑定进程]」",
            int(other.get("pid") or 0) == 0
            and not bool(other.get("process_bound")),
            "pid={}".format(other.get("pid")),
        )
        _check(
            "未绑定时武装开关**禁用**",
            not bool(other.get("arm_available")),
        )

        print("[4/4] 点一次【武装守护者】")
        armed = _post(
            base + "/api/sessions/{}/arm".format(snapshot.get("id")),
            {"armed": True},
        )
        _check(
            "打包版能真的武装守护者（psutil 在包里）",
            bool((armed or {}).get("ok")),
            str((armed or {}).get("error") or (armed or {}).get("message")),
        )
        disarmed = _post(
            base + "/api/sessions/{}/arm".format(snapshot.get("id")),
            {"armed": False},
        )
        _check(
            "解除武装也正常",
            bool((disarmed or {}).get("ok")),
            str((disarmed or {}).get("error") or ""),
        )
    finally:
        if exe_proc is not None:
            # ⚠️ 打包版是单文件模式 —— 必须杀**整棵进程树**，
            # 否则子进程会活下来继续占端口。
            _kill_tree(exe_proc.pid)
        for item in agents:
            try:
                item.terminate()
            except Exception as exc:  # noqa: BLE001
                print(
                    "结束子进程失败（PID {}）：{}: {}".format(
                        item.pid, type(exc).__name__, exc
                    ),
                    file=sys.stderr,
                )
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for item in checks if item["passed"])
    print("\n==== 打包版验证：{}/{} 通过 ====".format(passed, len(checks)))
    for item in checks:
        if not item["passed"]:
            print("  FAIL：{} —— {}".format(item["name"], item["detail"]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
