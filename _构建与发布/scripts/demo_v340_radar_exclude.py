"""v3.4.0 验收演示 —— 雷达「排除自身」与「自定义特征」。

它做的事**每一步都是真的**
==========================

1. 造一个**假的 Agent 家目录**（``.codex/sessions``），
   里面放三份日志：

   * ``sentinel-stress/`` —— 路径命中 ``sentinel`` 与 ``stress``；
   * ``test-run/``        —— 路径命中 ``test``；
   * ``normal-project/``  —— 路径干干净净，**该留着**。

2. 起**真的**哨兵（雷达模式），配置里的 ``radar.scan_roots``
   指到那个假家目录。

3. 先把 ``radar.exclude_keywords`` 配成 **空列表**
   （= 「我什么都不排除」）→ 调 ``/api/discover`` ——
   三份日志**全都**被扫出来（这就是「改配置**之前**」的样子）。

4. 把配置改成 ``[sentinel, stress, test]`` → 再调一次
   ``/api/discover`` —— 命中关键词的那两份**消失**了，
   只剩下 ``normal-project`` 那一份
   （这就是「改配置**之后**」的样子）。

5. 顺带验证**自定义进程特征**：配上 ``radar.custom_process_features:
   [neko]`` 之后，``/api/process_activity`` 能认出本机
   ``D:\\项目\\N.E.K.O`` 那个进程。

用法::

    python _构建与发布/scripts/demo_v340_radar_exclude.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

#: 一份「像模像样」的 Codex 会话日志的模板。
#:
#: ⚠️ 里面的 ``{sid}`` 必须**各不相同** ——
#: 雷达会把「同一个会话的多份文件」折叠成一条，
#: 三份日志共用一个 thread_id 的话，列表里只会剩下一条。
SESSION_TEMPLATE = [
    '{"type":"thread.started","thread_id":"{sid}"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0",'
    '"type":"agent_message","text":"已经在当前目录创建了 hi.py"}}',
]


def _session_lines(sid: str) -> List[str]:
    """造一份会话日志（每个会话的 id 不同）。

    :param sid: 会话 id。
    :return: 行内容列表。
    """
    # ⚠️ 用 replace 而不是 format ——
    #    JSON 里全是花括号，format 会把它们当成占位符。
    return [line.replace("{sid}", sid) for line in SESSION_TEMPLATE]


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


def _write_jsonl(path: str, lines: List[str]) -> None:
    """写一份 JSONL 日志。

    :param path: 目标路径。
    :param lines: 行内容。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _get_json(url: str, timeout: float = 30.0) -> Dict[str, Any]:
    """GET 一个接口并解析 JSON。

    :param url: 地址。
    :param timeout: 超时（秒）。
    :return: 解析出来的字典。
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _wait_http(url: str, timeout: float = 90.0) -> bool:
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


def _write_config(path: str, scan_root: str, exclude: List[str]) -> str:
    """写一份演示用的配置。

    :param path: 配置路径。
    :param scan_root: 雷达扫描根目录（绝对路径）。
    :param exclude: ``radar.exclude_keywords`` 的值。
    :return: 配置文本。
    """
    lines = [
        "radar:",
        "  scan_roots:",
        '    - "{}"'.format(scan_root.replace("\\", "/")),
    ]
    if exclude:
        lines.append("  exclude_keywords:")
        lines.extend("    - {}".format(item) for item in exclude)
    else:
        # ⚠️ 空清单要写成一行 ``[]`` ——
        #    「什么都不排除」与「没配、用默认清单」是两回事。
        lines.append("  exclude_keywords: []")
    lines.extend([
        "  custom_process_features:",
        "    - neko",
        "",
    ])
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


def main(argv=None) -> int:
    """命令行入口。

    :param argv: 参数列表。
    :return: 进程退出码。
    """
    _prepare_stdout()
    parser = argparse.ArgumentParser(
        description="v3.4.0 雷达排除自身验收演示"
    )
    parser.add_argument("--port", type=int, default=8143, help="看板端口")
    args = parser.parse_args(argv)

    root = _project_root()
    work = os.path.join(root, "logs", "v340-demo")
    os.makedirs(work, exist_ok=True)

    # 假的 Agent 家目录 —— 里面放三份日志。
    fake_home = os.path.join(work, "fakehome")
    codex_root = os.path.join(fake_home, ".codex")
    sessions = os.path.join(codex_root, "sessions")
    if os.path.isdir(fake_home):
        shutil.rmtree(fake_home)
    samples = {
        "sentinel-stress": (
            os.path.join(sessions, "sentinel-stress", "rollout-1.jsonl"),
            "01a17af0-0001",
        ),
        "test-run": (
            os.path.join(sessions, "test-run", "rollout-2.jsonl"),
            "01a17af0-0002",
        ),
        "normal-project": (
            os.path.join(sessions, "normal-project", "rollout-3.jsonl"),
            "01a17af0-0003",
        ),
    }
    for path, sid in samples.values():
        _write_jsonl(path, _session_lines(sid))
    print("假家目录：{}".format(codex_root))
    for name, (path, _sid) in samples.items():
        print("  · {} → {}".format(name, path))

    config = os.path.join(work, "radar-exclude-config.yaml")
    port = int(args.port)
    base = "http://127.0.0.1:{}".format(port)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server_log = open(
        os.path.join(work, "radar-server.log"), "w", encoding="utf-8"
    )
    attached = os.path.join(work, "attached_sessions.json")
    if os.path.exists(attached):
        os.remove(attached)

    # ① 先配成「什么都不排除」。
    config_before = _write_config(config, codex_root, [])
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--radar",
            "--config", config,
            "--alert-log", os.path.join(work, "radar-alert.log"),
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
    evidence: Dict[str, Any] = {"port": port, "scan_root": codex_root}
    try:
        print("[1/3] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("看板没起来")
            return 1

        print("[2/3] 改配置前 —— exclude_keywords 为空 …")
        before = _get_json(base + "/api/discover")
        before_paths = [
            item.get("path", "") for item in before.get("sessions", [])
        ]
        evidence["before"] = {
            "config": config_before,
            "exclude_keywords": [],
            "count": before.get("count"),
            "paths": before_paths,
        }
        print("      扫到 {} 条：".format(before.get("count")))
        for path in before_paths:
            print("        · {}".format(path))

        print("[3/3] 改配置后 —— exclude_keywords = sentinel/stress/test …")
        config_after = _write_config(
            config, codex_root, ["sentinel", "stress", "test"]
        )
        after = _get_json(base + "/api/discover")
        after_paths = [
            item.get("path", "") for item in after.get("sessions", [])
        ]
        effective = _get_json(base + "/api/config").get(
            "radar_exclude_keywords"
        )
        evidence["after"] = {
            "config": config_after,
            "exclude_keywords": effective,
            "count": after.get("count"),
            "paths": after_paths,
        }
        print("      当前实际生效的排除清单：{}".format(effective))
        print("      扫到 {} 条：".format(after.get("count")))
        for path in after_paths:
            print("        · {}".format(path))

        # 顺带验证自定义进程特征。
        activity = _get_json(base + "/api/process_activity")
        custom = [
            item
            for item in activity.get("active", [])
            if str(item.get("agent") or "") == "neko"
        ]
        evidence["custom_features"] = {
            "configured": ["neko"],
            "neko_processes": [
                {"pid": item.get("pid"), "cwd": item.get("cwd")}
                for item in custom
            ],
        }
        print("      自定义特征 neko 认出的进程：{}".format(
            len(custom)
        ))
        for item in custom:
            print("        · pid={} cwd={}".format(
                item.get("pid"), item.get("cwd")
            ))

        out = os.path.join(
            root,
            "_管理文档",
            "文档与报告",
            "assets",
            "v340-radar-exclude-evidence.json",
        )
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("      证据 → {}".format(out))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        server_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
