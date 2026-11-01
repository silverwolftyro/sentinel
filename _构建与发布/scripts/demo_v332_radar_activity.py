"""v3.3.2 验收演示 —— 雷达活跃进程感知 + 快捷重定向命令。

它做的事**每一步都是真的**：

1. 在**独立的演示目录**里准备一份配置 —— 不碰用户自己的
   ``config/attached_sessions.json``，所以挂载状态一定是「空的」；
2. 起**真的**看板（``src.gui.server``，雷达模式）；
3. 后端扫本机进程 → 发现有活跃的 Agent 进程、却对不上任何正在监控的日志
   → ``/api/process_activity`` 返回 ``unmonitored_count ≥ 1``；
4. 前端把这条信息渲染成看板**左侧**的橘黄提示；
5. CDP 驱动 Edge 拍两张实拍图：
   ① 看板左侧的「检测到活跃进程」提示；
   ② 设置页【挂载管理】里的「快捷重定向命令」。

⚠️ 这一版**不需要**造假日志 ——
   提示要证明的是「进程在、日志没挂上」，而这件事本机就成立
   （你自己正在用的那个 Agent 就是那个进程）。

用法::

    python _构建与发布/scripts/demo_v332_radar_activity.py
"""

from __future__ import annotations

import json
import os
import shutil
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


def _wait_http(url: str, timeout: float = 60.0) -> bool:
    """等一个 URL 能通。

    :param url: 目标地址。
    :param timeout: 最长等多少秒。
    :return: 通了返回 ``True``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.6)
    return False


def _get_json(url: str) -> dict:
    """GET 一个 JSON 接口。

    :param url: 目标地址。
    :return: 解析后的字典；失败就是空字典。
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def _shoot(
    root: str,
    url: str,
    out_name: str,
    cdp_port: int,
    eval_js: str = "",
    scroll_to: str = "",
    wait: float = 7.0,
) -> str:
    """拍一张看板图。

    ⚠️ **每张图用不同的 CDP 端口** ——
    上一轮的无头 Edge 可能还没退干净，
    端口被占住时第二次连接会**一直等到超时**。

    :param root: 项目根目录。
    :param url: 要打开的地址（**完整** URL，含路径）。
    :param out_name: 输出文件名（放在管理文档的 assets 目录）。
    :param cdp_port: CDP 调试端口。
    :param eval_js: 截图前回读的一段 JS。
    :param scroll_to: 截图前滚动到哪个元素（CSS 选择器）。
    :param wait: 页面打开后等多久再拍。
    :return: 截图路径。
    """
    assets = os.path.join(root, "_管理文档", "文档与报告", "assets")
    os.makedirs(assets, exist_ok=True)
    shot = os.path.join(assets, out_name)
    command = [
        sys.executable,
        os.path.join(root, "_构建与发布", "scripts", "capture_dashboard.py"),
        "--url", url,
        "--out", shot,
        "--port", str(cdp_port),
        "--width", "1440",
        "--height", "1000",
        "--scale", "2",
        "--wait", str(wait),
    ]
    if scroll_to:
        command.extend(["--scroll-to", scroll_to])
    if eval_js:
        command.extend(["--eval", eval_js])
    result = subprocess.run(
        command,
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
    work = os.path.join(root, "logs", "v332-demo")
    os.makedirs(work, exist_ok=True)

    # ⚠️ 演示必须用**独立的配置** ——
    # ``--config`` 决定了挂载历史文件的位置（``<配置目录>/attached_sessions.json``），
    # 用独立配置就绝不会碰到用户自己的挂载状态。
    config = os.path.join(work, "config.yaml")
    example = os.path.join(root, "config", "config.example.yaml")
    if os.path.exists(example):
        shutil.copyfile(example, config)
    else:
        with open(config, "w", encoding="utf-8") as handle:
            handle.write("alert:\n  enabled: false\n")
    attached = os.path.join(work, "attached_sessions.json")
    if os.path.exists(attached):
        os.remove(attached)

    alert = os.path.join(work, "alert.log")
    if os.path.exists(alert):
        os.remove(alert)

    port = 8141
    base = "http://127.0.0.1:{}".format(port)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # ⚠️ 子进程的输出**写文件**，不要接 PIPE ——
    # 没人读的 PIPE 会被写满（64 KiB），写满之后服务进程会卡在写日志上。
    server_log = open(
        os.path.join(work, "server.log"), "w", encoding="utf-8"
    )
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--radar",
            "--config", config,
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

        print("[2/4] 查活跃进程 …")
        data = _get_json(base + "/api/process_activity")
        print("      可用：{}".format(data.get("available")))
        print("      活跃 Agent 进程：{} 个".format(
            data.get("active_count")
        ))
        print("      其中**没被监控**的：{} 个".format(
            data.get("unmonitored_count")
        ))
        print("      分项：{}".format(data.get("by_agent")))
        print("      捕获文件：{}".format(data.get("capture_path")))

        print("[3/4] 拍看板左侧的提示 …")
        _shoot(
            root,
            base + "/",
            "v332-radar-activity.png",
            cdp_port=9421,
            eval_js=(
                "JSON.stringify({hidden:"
                "document.getElementById('radar-alert').hidden,"
                "title:document.getElementById('radar-alert-title')"
                ".textContent})"
            ),
        )

        print("[4/4] 拍设置页的快捷重定向命令 …")
        _shoot(
            root,
            base + "/settings/sessions",
            "v332-capture-command.png",
            cdp_port=9422,
            scroll_to="#capture-command",
            eval_js=(
                "JSON.stringify({cmd:"
                "document.getElementById('capture-command-text')"
                ".textContent})"
            ),
        )
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
