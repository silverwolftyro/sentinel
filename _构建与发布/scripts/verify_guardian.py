"""守护者端到端自检 —— 可重复运行的验收脚本。

这个脚本会真的挂起一个**自己启动的**子进程（一个 sleep），
验证整条链路：

    模拟 Agent 写红线命令
        → 规则层判定 red_line
        → 守护者挂起目标进程
        → 看板拿到告警
        → 用户放行（override）
        → 进程恢复 + 规则拉黑 + 留痕

脚本自始至终只操作**自己创建的**进程，绝不碰用户的任何东西。

用法::

    python _构建与发布/scripts/verify_guardian.py
    python _构建与发布/scripts/verify_guardian.py --port 8123

依赖：``psutil``（守护者本身需要）、``pyyaml``。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import psutil
import yaml


def _find_project_root() -> str:
    """向上找到项目根目录（含 ``src/core`` 的那一层）。

    脚本放在 ``_构建与发布/scripts/`` 下，**不能**靠固定的层数往上数 ——
    目录一调整就会指错地方。所以按特征找：含有 ``src/core`` 的目录就是根。
    """
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

#: 模拟的 Agent 日志 —— 一条足以触发红线的命令。
DEMO_LOG = (
    '{"type": "thinking", "thinking": "我打算一条命令清空根目录。"}\n'
    '{"type": "tool_call", "tool_call": {"name": "Bash", '
    '"arguments": {"command": "rm -rf /"}}}\n'
)


def _base_config(workdir: str) -> dict:
    """构造演示用的配置。"""
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
        "llm": {"enabled": False},
        "alert": {
            "enabled": True,
            "min_level": "info",
            "channels": [
                {"type": "file", "enabled": True,
                 "path": os.path.join(workdir, "alerts.jsonl")},
                {"type": "desktop", "enabled": False},
                {"type": "sound", "enabled": False},
                {"type": "webhook", "enabled": False, "url": ""},
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
        "logging": {"level": "INFO", "dir": workdir},
    }


def _post(url: str, payload: dict) -> dict:
    """POST 一个 JSON。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str) -> dict:
    """GET 一个 JSON。"""
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_server(url: str, timeout: float = 30.0) -> bool:
    """等看板起来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _get(url)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.4)
    return False


def main() -> int:
    """跑一遍端到端自检。"""
    parser = argparse.ArgumentParser(
        description="哨兵守护者端到端自检"
    )
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="sentinel-guardian-")
    log_path = os.path.join(workdir, "agent.jsonl")
    config_path = os.path.join(workdir, "config.yaml")
    alert_log = os.path.join(workdir, "sentinel_alert.log")

    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(DEMO_LOG)

    # --- 1) 启动「被监控进程」（一个可被挂起的 sleep）---
    agent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    print(f"[1/7] 被监控进程已启动：PID={agent.pid}")

    config = _base_config(workdir)
    config["guardian"]["target_pid"] = agent.pid
    with open(config_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)

    # --- 2) 启动看板 ---
    server = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--watch", log_path,
            "--alert-log", alert_log,
            "--config", config_path,
            "--port", str(args.port),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{args.port}"
    try:
        if not _wait_server(f"{base}/api/state"):
            print("看板启动失败", file=sys.stderr)
            return 1
        print(f"[2/7] 看板已启动：{base}")

        status = _get(f"{base}/api/guardian/status")
        print(
            f"[3/7] 守护者状态：enabled={status.get('enabled')} "
            f"target_pid={status.get('target_pid')}"
        )
        if not status.get("enabled"):
            print("守护者未启用，自检失败", file=sys.stderr)
            return 1

        # --- 4) 触发扫描，红线应当触发挂起 ---
        _post(f"{base}/api/scan", {})
        time.sleep(2.0)
        agent_state = psutil.Process(agent.pid).status()
        print(f"[4/7] 扫描后被监控进程状态 = {agent_state}")
        if agent_state != "stopped":
            print("守护者没有挂起进程，自检失败", file=sys.stderr)
            return 1

        # --- 5) 看板应当能看到这条红线告警 ---
        alerts = _get(f"{base}/api/alerts").get("alerts") or []
        redline = [
            item for item in alerts
            if item.get("severity") == "red_line"
        ]
        print(
            f"[5/7] 看板告警 {len(alerts)} 条，其中红线 {len(redline)} 条"
        )
        if not redline:
            print("看板没有红线告警，自检失败", file=sys.stderr)
            return 1

        # --- 6) 用户放行 ---
        result = _post(
            f"{base}/api/guardian/override",
            {"rule_id": redline[0].get("rule_id"), "source": "verify"},
        )
        time.sleep(1.0)
        after = psutil.Process(agent.pid).status()
        print(
            f"[6/7] 放行后：ok={result.get('ok')} "
            f"resumed={result.get('resumed')} 进程状态={after}"
        )
        if after != "running":
            print("放行没有恢复进程，自检失败", file=sys.stderr)
            return 1

        # --- 7) 规则拉黑 + 留痕 ---
        final = _get(f"{base}/api/guardian/status")
        rules = final.get("overridden_rules") or []
        audit = os.path.join(workdir, "guardian.log")
        trace = os.path.join(workdir, "guardian_overrides.json")
        has_audit = os.path.exists(audit) and os.path.exists(trace)
        print(
            f"[7/7] 已放行规则 = {rules}；留痕文件齐全 = {has_audit}"
        )
        if not rules or not has_audit:
            print("规则未拉黑或缺少留痕，自检失败", file=sys.stderr)
            return 1

        print("\n✅ 守护者端到端自检全部通过")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        if agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()


if __name__ == "__main__":
    raise SystemExit(main())
