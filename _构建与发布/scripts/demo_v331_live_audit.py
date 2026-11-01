"""v3.3.1 验收演示 —— 实时审核流水 + 无目标强引导。

它做的事很简单，但**每一步都是真的**：

1. 造一份**真的** MCode 格式日志（里面全是正常的工具调用）；
2. 起**真的**看板（``src.gui.server``），让它自动扫描这份日志；
3. 引擎判定为 ``ON_TRACK`` → 写 ``ONTRACK_JSON`` 行 → 看板尾部器读到
   → SSE 推给前端 → 「实时审核流水」一行一行滚出来；
4. CDP 驱动 Edge 拍照（``capture_dashboard.py`` 的那套）。

⚠️ 演示日志里**故意不放**用户消息 ——
放了会被 v3.0.0 的自动锚定接管，
「未设置」的强引导就拍不到了（那是另一张图的事）。

用法::

    python _构建与发布/scripts/demo_v331_live_audit.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _prepare_stdout() -> None:
    """控制台可能是 GBK —— 先切 UTF-8。"""
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


def _project_root() -> str:
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


#: 演示用的「正常动作」序列 —— 全是 ON_TRACK，一次危险动作都没有。
NORMAL_ACTIONS = [
    ("Read", {"file_path": "src/core/engine.py"}),
    ("Read", {"file_path": "src/gui/server.py"}),
    ("Bash", {"command": "python -m pytest -q tests/"}),
    ("Grep", {"pattern": "ON_TRACK"}),
    ("Glob", {"pattern": "**/*.yaml"}),
    ("Read", {"file_path": "config/config.example.yaml"}),
    ("Bash", {"command": "ruff check ."}),
    ("Read", {"file_path": "src/rules/basic.py"}),
    ("Bash", {"command": "git status"}),
    ("Edit", {"file_path": "src/core/live_audit.py"}),
    ("Read", {"file_path": "docs/README.md"}),
    ("Bash", {"command": "pytest -q tests/test_v331_live_audit.py"}),
]


def _write_demo_log(path: str) -> str:
    """写一份演示用的 MCode 日志（**不含**用户消息）。

    :param path: 目标路径。
    :return: 路径字符串。
    """
    lines = []
    for name, arguments in NORMAL_ACTIONS:
        lines.append({
            "type": "tool_call",
            "tool_call": {"name": name, "arguments": arguments},
        })
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    return path


def _wait_http(url: str, timeout: float = 40.0) -> bool:
    """等一个 URL 能通。

    :param url: 目标地址。
    :param timeout: 最长等多少秒。
    :return: 通了返回 ``True``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.6)
    return False


def _post(url: str, payload=None) -> str:
    """POST 一个 JSON 请求。

    :param url: 目标地址。
    :param payload: 请求体（``None`` 表示空对象）。
    :return: 响应正文。
    """
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def _shoot(
    root: str,
    base: str,
    out_name: str,
    wait: float = 6.0,
    cdp_port: int = 9333,
) -> str:
    """拍一张看板图。

    ⚠️ **每张图用不同的 CDP 端口** ——
    上一轮的无头 Edge 可能还没退干净，
    端口被占住时第二次连接会**一直等到超时**。

    :param root: 项目根目录。
    :param base: 看板地址。
    :param out_name: 输出文件名（放在管理文档的 assets 目录）。
    :param wait: 页面打开后等多久再拍。
    :param cdp_port: CDP 调试端口。
    :return: 截图路径。
    """
    assets = os.path.join(root, "_管理文档", "文档与报告", "assets")
    os.makedirs(assets, exist_ok=True)
    shot = os.path.join(assets, out_name)
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(
                root, "_构建与发布", "scripts", "capture_dashboard.py"
            ),
            "--url", base + "/",
            "--out", shot,
            "--port", str(cdp_port),
            "--width", "1440",
            "--height", "1000",
            "--scale", "2",
            "--wait", str(wait),
            "--eval",
            "document.querySelectorAll('.live-audit__line').length",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(result.stdout or "")
    if result.stderr:
        print(result.stderr)
    print("      截图 → {}".format(shot))
    return shot


def main(argv=None) -> int:
    """命令行入口。

    :param argv: 参数列表（这里不用）。
    :return: 进程退出码。
    """
    _prepare_stdout()
    root = _project_root()
    work = os.path.join(root, "logs", "v331-demo")
    os.makedirs(work, exist_ok=True)
    watch = os.path.join(work, "mcode.jsonl")
    alert = os.path.join(work, "sentinel_alert.log")
    for path in (watch, alert):
        if os.path.exists(path):
            os.remove(path)
    _write_demo_log(watch)

    port = 8131
    base = "http://127.0.0.1:{}".format(port)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # ⚠️ 子进程的输出**写文件**，不要接 PIPE ——
    # 没人读的 PIPE 会被写满（64 KiB），
    # 写满之后服务进程会**卡在写日志上**，
    # 表现就是「看板半死不活、CDP 一直等不到回包」。
    server_log = open(
        os.path.join(work, "server.log"), "w", encoding="utf-8"
    )
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--watch", watch,
            "--alert-log", alert,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--no-browser",
            "--no-health",
            "--no-guardian",
        ],
        cwd=root,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        print("[1/4] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("看板没起来")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/4] 触发扫描（引擎会把 ON_TRACK 写成流水行）…")
        try:
            print("      " + _post(base + "/api/scan")[:160])
        except (urllib.error.URLError, OSError) as exc:
            print("      扫描接口没响应：{}".format(exc))

        print("[3/4] 等流水滚出来 …")
        deadline = time.time() + 30
        count = 0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    base + "/api/state", timeout=5
                ) as response:
                    state = json.loads(response.read().decode("utf-8"))
                count = len(state.get("ontrack") or [])
            except (urllib.error.URLError, OSError, ValueError):
                count = 0
            if count >= 8:
                break
            time.sleep(1.0)
        print("      流水条目：{} 条".format(count))

        print("[4/4] 拍照 …")
        # 第一张：没设目标 —— 橘黄高亮 + 气泡引导 + 流水在滚。
        _shoot(root, base, "v331-live-audit.png", cdp_port=9333)

        print("      设定目标 → 顶部应变「手动锚定」…")
        try:
            _post(
                base + "/api/sessions/default/task_goal",
                {"goal": "重构 auth 模块并修复登录 bug"},
            )
        except (urllib.error.URLError, OSError) as exc:
            print("      设定目标没成功：{}".format(exc))
        time.sleep(3)
        # 第二张：手动锚定之后 —— 气泡消失，来源变成「手动锚定」。
        _shoot(root, base, "v331-goal-anchored.png", cdp_port=9334)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        server_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
