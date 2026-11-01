"""v3.5.0 验收演示 —— 多流隔离 · 极简 L2 Prompt · 级联双盲。

它做的事**每一步都是真的**
==========================

1. **两路拆分** ——
   造两个**不同工作区**的 Agent 日志（外加一份「同一个文件里混着两个
   Agent」的日志），起**真的**哨兵（雷达模式），用 ``POST /api/attach``
   真的把它们挂进去，然后从 ``/api/sessions`` 与 ``/api/sessions/{id}/state``
   里读出**指纹分路**的结果 —— 证明看板确实拆成了多路。

2. **极简 L2 Prompt** ——
   用**真的** :class:`~src.llm.cascade.CascadeRouter` 跑两次 L2 判定
   （两次的输入只差一个时间戳），把两次的 system 前缀与 user 提示
   **原样打印**出来，并当场比对 —— 证明「静态前缀逐字不变」
   （Prefix Cache 能命中）。

3. **L2 → L3 触发** ——
   用 **Mock** 提供方跑三条剧本：

   * L2 判 ``ON_TRACK`` → **不调云端**，省下一次调用；
   * L2 判 ``DRIFT`` → **立刻调 L3**，双盲命中 → 亮红灯；
   * L2 不可用（429）→ **立刻回退 L1**，不调云端。

   最后再用**真的**引擎跑一遍，把 ``ALERT_UPDATE_JSON``（升级行）
   与 ``CASCADE_JSON``（统计行）从告警日志里抓出来。

用法::

    python _构建与发布/scripts/demo_v350_multistream_cascade.py
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

#: 仓库根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 让 ``import src`` 能用（脚本在 ``_构建与发布/scripts/`` 下）。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 假 Agent 的两行事件模板（带 ``cwd`` —— 指纹就从这里来）。
AGENT_LINES = [
    '{"type":"user_message","cwd":"{cwd}","content":"帮我看看 {proj} 里的配置"}',
    '{"type":"tool_call","cwd":"{cwd}","tool_call":'
    '{"name":"Read","arguments":{"file_path":"{proj}/config.yaml"}}}',
    '{"type":"tool_call","cwd":"{cwd}","tool_call":'
    '{"name":"Bash","arguments":{"command":"ls -la {proj}"}}}',
    '{"type":"tool_result","cwd":"{cwd}","tool_result":'
    '{"status":"ok","output":"config.yaml  main.py"}}',
]


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


def _write_jsonl(path: str, lines: List[str]) -> None:
    """写一份 JSONL 日志。

    :param path: 目标路径。
    :param lines: 行内容。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _append_jsonl(path: str, lines: List[str]) -> None:
    """**追加**一行行日志（模拟 Agent 正在写）。

    ⚠️ 挂载进来的会话是 ``tail=True`` 跟随的 ——
    它**从挂载那一刻的末尾**开始读。
    所以演示必须**先挂载、后追加**，
    追加的内容才会被真的读到。

    :param path: 目标路径。
    :param lines: 行内容。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()


def _agent_lines(cwd: str, project: str) -> List[str]:
    """造一份「某个工作区里的 Agent」日志。

    :param cwd:    工作目录（绝对路径）。
    :param project: 项目名。
    :return: 行内容列表。
    """
    return [
        line.replace("{cwd}", cwd.replace("\\", "/")).replace(
            "{proj}", project
        )
        for line in AGENT_LINES
    ]


def _get_json(url: str, timeout: float = 30.0) -> Dict[str, Any]:
    """GET 一个接口并解析 JSON。

    :param url: 地址。
    :param timeout: 超时（秒）。
    :return: 解析出来的字典。
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 30.0):
    """POST 一个 JSON。

    :param url: 地址。
    :param payload: 请求体。
    :param timeout: 超时（秒）。
    :return: 解析出来的字典。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
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


def _capture(
    url: str,
    out: str,
    cdp_port: int,
    wait: float = 6.0,
    width: int = 1600,
    height: int = 1000,
    click: str = "",
    click_wait: float = 3.0,
    scroll_to: str = "",
) -> bool:
    """用**真的**无头浏览器截一张图。

    :param url: 页面地址。
    :param out: 输出路径。
    :param cdp_port: CDP 端口。
    :param wait: 打开后等多久（秒）。
    :param width: 视口宽。
    :param height: 视口高。
    :param click: 截图前点一下的选择器。
    :param click_wait: 点完等多久（秒）。
    :param scroll_to: 截图前滚到哪个元素。
    :return: 截成功返回 ``True``。
    """
    script = os.path.join(ROOT, "_构建与发布", "scripts", "capture_dashboard.py")
    cmd = [
        sys.executable, script,
        "--url", url,
        "--out", out,
        "--port", str(cdp_port),
        "--width", str(width),
        "--height", str(height),
        "--scale", "2",
        "--wait", str(wait),
        "--probe",
    ]
    if scroll_to:
        cmd += ["--scroll-to", scroll_to]
    if click:
        cmd += ["--click", click, "--click-wait", str(click_wait)]
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print("      截图失败：{}: {}".format(type(exc).__name__, exc))
        return False
    if result.returncode != 0:
        print("      截图失败（退出码 {}）".format(result.returncode))
        return False
    print("      截图 → {}".format(out))
    return True


# ===========================================================================
# 演示 1：两路拆分
# ===========================================================================
def demo_streams(port: int, work: str) -> Dict[str, Any]:
    """挂两个不同工作区的 Agent，证明看板拆成了多路。

    :param port: 看板端口。
    :param work: 工作目录（放假日志与告警日志）。
    :return: 证据字典。
    """
    base = "http://127.0.0.1:{}".format(port)
    alpha_dir = os.path.join(work, "workspace-alpha")
    beta_dir = os.path.join(work, "workspace-beta")
    os.makedirs(alpha_dir, exist_ok=True)
    os.makedirs(beta_dir, exist_ok=True)

    alpha_log = os.path.join(work, "alpha.jsonl")
    beta_log = os.path.join(work, "beta.jsonl")
    shared_log = os.path.join(work, "shared-mixed.jsonl")

    # ⚠️ **先只写第一行** ——
    #    挂载进来的会话是 ``tail=True`` 跟随的：
    #    它从挂载那一刻的末尾开始读。
    #    所以「第一行」负责自动提取任务目标，
    #    真正要证明隔离的那几行必须**挂载之后**再追加。
    alpha_head = _agent_lines(alpha_dir, "alpha-proj")[:1]
    beta_head = _agent_lines(beta_dir, "beta-proj")[:1]
    alpha_body = _agent_lines(alpha_dir, "alpha-proj")[1:]
    beta_body = _agent_lines(beta_dir, "beta-proj")[1:]
    _write_jsonl(alpha_log, alpha_head)
    _write_jsonl(beta_log, beta_head)
    # ⚠️ 这一份是**重点** —— 同一个文件里混着两个工作区的 Agent。
    _write_jsonl(shared_log, alpha_head)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # ⚠️ 挂载会话的告警日志落在 ``logs/attached/`` 下，**跨次演示会累积** ——
    #    演示前先清掉，免得上一轮的流水混进这一轮的计数里。
    attached_dir = os.path.join(ROOT, "logs", "attached")
    if os.path.isdir(attached_dir):
        for name in os.listdir(attached_dir):
            stem = name.split("-")[0]
            if stem in ("alpha", "beta", "shared", "shared-mixed"):
                try:
                    os.remove(os.path.join(attached_dir, name))
                except OSError:
                    pass
    server_log = open(
        os.path.join(work, "v350-server.log"), "w", encoding="utf-8"
    )
    attached = os.path.join(work, "attached_sessions.json")
    if os.path.exists(attached):
        os.remove(attached)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--radar",
            "--config", os.path.join(work, "v350-config.yaml"),
            "--alert-log", os.path.join(work, "v350-alert.log"),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--no-browser",
            "--no-health",
            "--no-guardian",
        ],
        cwd=ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    evidence: Dict[str, Any] = {"port": port}
    try:
        print("[1/5] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return evidence

        print("[2/5] 挂载三个会话（两个工作区 + 一份混合日志）…")
        for path, agent, cwd in (
            (alpha_log, "mcode", alpha_dir),
            (beta_log, "mcode", beta_dir),
            (shared_log, "mcode", work),
        ):
            result = _post_json(
                base + "/api/attach",
                {"path": path, "agent": agent, "cwd": cwd},
            )
            print(
                "      · {} → 会话 {}（{}）".format(
                    os.path.basename(path),
                    result.get("id"),
                    "成功" if result.get("ok", True) else result.get("error"),
                )
            )
        # 让跟随线程把历史读完。
        time.sleep(3.0)

        print("[3/5] 追加事件（模拟 Agent 正在写）…")
        _append_jsonl(alpha_log, alpha_body)
        _append_jsonl(beta_log, beta_body)
        # ⚠️ 混合日志：**先写 A 路的，再写 B 路的** ——
        #    同一份日志里的两路必须是交织的，才谈得上「串台」。
        _append_jsonl(shared_log, alpha_body)
        _append_jsonl(shared_log, beta_body)
        time.sleep(3.0)

        print("[4/5] 读回每个会话的指纹分路 …")
        sessions = _get_json(base + "/api/sessions").get("sessions", [])
        streams_report = []
        for session in sessions:
            state = _get_json(
                base + "/api/sessions/{}/state".format(session["id"])
            )
            streams = state.get("streams") or []
            streams_report.append(
                {
                    "id": session["id"],
                    "label": session.get("label"),
                    "workspace": session.get("workspace"),
                    "streams": streams,
                }
            )
            print(
                "      · {}（工作区 {}）→ 分成 {} 路：".format(
                    session.get("label"),
                    os.path.basename(str(session.get("workspace") or "")),
                    len(streams),
                )
            )
            for item in streams:
                print(
                    "          · {} → {} 告警 / {} 正常".format(
                        item.get("label"),
                        item.get("alerts"),
                        item.get("ontrack"),
                    )
                )
        evidence["sessions"] = streams_report

        print("[5/5] 验证各路互不干扰（同一份日志里的两路）…")
        mixed = None
        for item in streams_report:
            if len(item["streams"]) > 1:
                mixed = item
                break
        if mixed is not None:
            keys = [item["key"] for item in mixed["streams"]]
            print("      混合日志被拆成 {} 路：{}".format(len(keys), keys))
            evidence["mixed_stream_keys"] = keys
            evidence["mixed_streams"] = mixed["streams"]
        else:
            print("      ⚠️ 没有抓到多路会话（混合日志没被拆开）")
            evidence["mixed_streams"] = []

        print("[6/6] 截图（看板 + 设置页的 L2/L3 两个区）…")
        assets = os.path.join(
            ROOT, "_管理文档", "文档与报告", "assets"
        )
        os.makedirs(assets, exist_ok=True)
        evidence["screenshots"] = {
            "dashboard": _capture(
                base + "/",
                os.path.join(assets, "v350-dashboard-streams.png"),
                cdp_port=port + 100,
                # ⚠️ 点到**第 4 张**会话卡片（那份「同一文件混两个 Agent」的）——
                #    这样右侧面板里能看到它真的在跑。
                click="li.session-item:nth-of-type(4)",
                click_wait=3.5,
            ),
            "settings": _capture(
                base + "/settings/llm",
                os.path.join(assets, "v350-settings-cascade.png"),
                cdp_port=port + 101,
                height=1500,
                # ⚠️ 展开「②-2 L3 云端」并滚到它 ——
                #    两个配置区**同时**看得见，才叫「分离得清楚」。
                click="#cascade-l3-details > summary",
                click_wait=1.5,
                scroll_to="#cascade-l3-details",
            ),
        }
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        server_log.close()
    return evidence


# ===========================================================================
# 演示 2：极简 L2 Prompt
# ===========================================================================
class _CapturingProvider:
    """假模型提供方 —— 记录每次调用的 system / user。

    :param verdicts: 依次返回的判定文本。
    :param raise_with: 抛这个异常（模拟 429 / 503 / 超时）。
    """

    def __init__(self, verdicts=None, raise_with=None):
        self.verdicts = list(verdicts or [])
        self.raise_with = raise_with
        self.calls = []
        self.model = "bonsai2-27b"

    def chat(self, system: str, user: str) -> str:
        """记录并返回判定文本。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: 判定文本。
        """
        self.calls.append((system, user))
        if self.raise_with is not None:
            raise self.raise_with
        if self.verdicts:
            return self.verdicts.pop(0)
        return '{"verdict":"ON_TRACK","reason":"在读文件"}'


def demo_prompt() -> Dict[str, Any]:
    """展示 L2 的极简 Prompt，并证明静态前缀逐字不变。

    :return: 证据字典。
    """
    from src.llm.cascade import CascadeRouter

    provider = _CapturingProvider(
        [
            '{"verdict":"ON_TRACK","reason":"在读配置"}',
            '{"verdict":"ON_TRACK","reason":"在读配置"}',
        ]
    )
    router = CascadeRouter(provider, None, goal="检查 alpha-proj 的配置")

    # 两次调用只有时间戳不同 —— 这正是「易变内容」。
    router.filter(
        summary="2026-10-29 23:41:05 第1步 [Read] alpha-proj/config.yaml",
        action="Read alpha-proj/config.yaml",
    )
    router.filter(
        summary="2026-10-30 00:00:00 第1步 [Read] alpha-proj/config.yaml",
        action="Read alpha-proj/config.yaml",
    )

    system_a, user_a = provider.calls[0]
    system_b, user_b = provider.calls[1]

    print("\n──── L2 的 system（静态前缀）────")
    print(system_a)
    print("──── L2 的 user（易变部分，已剥离）────")
    print(user_a)

    same_prefix = system_a == system_b
    same_user = user_a == user_b
    print("")
    print("两次调用的 system 前缀逐字相同：{}".format(same_prefix))
    print("两次调用的 user 也相同（时间戳已被剥离）：{}".format(same_user))
    print("user 提示长度：{} 字符".format(len(user_a)))

    return {
        "system": system_a,
        "user": user_a,
        "system_bytes": len(system_a.encode("utf-8")),
        "user_bytes": len(user_a.encode("utf-8")),
        "prefix_identical": same_prefix,
        "user_identical": same_user,
        "raw_timestamp_kept": "2026-10-29 23:41:05" in user_a,
    }


# ===========================================================================
# 演示 3：L2 → L3 触发（Mock）
# ===========================================================================
def demo_cascade() -> Dict[str, Any]:
    """用 Mock 提供方跑三条级联剧本。

    :return: 证据字典。
    """
    from src.llm.cascade import CascadeRouter

    report: Dict[str, Any] = {}

    print("\n[剧本 A] L2 判正常 —— 必须静默，不调云端")
    l2 = _CapturingProvider(['{"verdict":"ON_TRACK","reason":"在读配置"}'])
    l3 = _CapturingProvider([])
    router = CascadeRouter(l2, l3)
    result = router.filter(summary="s", action="ls")
    print("      状态={}  L3 调用次数={}  省下云端调用={}".format(
        result.status, len(l3.calls), router.saved_cloud_calls
    ))
    report["on_track"] = {
        "status": result.status,
        "l3_calls": len(l3.calls),
        "saved_cloud_calls": router.saved_cloud_calls,
    }

    print("\n[剧本 B] L2 报警 —— 必须叫醒 L3，双盲命中才亮红灯")
    l2 = _CapturingProvider(['{"verdict":"DRIFT","reason":"跑偏了"}'])
    l3 = _CapturingProvider(['{"verdict":"DANGEROUS","reason":"确实危险"}'])
    router = CascadeRouter(l2, l3)
    result = router.filter(
        summary="s",
        action="rm -rf /",
        l1_verdict="DANGEROUS",
        l1_reason="命中危险命令",
    )
    print("      状态={}  红灯={}  L3 调用次数={}".format(
        result.status, result.red_light, len(l3.calls)
    ))
    if l3.calls:
        print("      ↓ L3 收到的 prompt（节选）")
        for line in l3.calls[0][1].splitlines()[:12]:
            print("        " + line)
    report["escalated"] = {
        "status": result.status,
        "red_light": result.red_light,
        "l3_calls": len(l3.calls),
        "l3_prompt": l3.calls[0][1] if l3.calls else "",
    }

    print("\n[剧本 C] L2 不可用（429）—— 必须立刻回退 L1，不调云端")
    l2 = _CapturingProvider(
        raise_with=RuntimeError("HTTP 429：触发限流（稍后重试）")
    )
    l3 = _CapturingProvider([])
    router = CascadeRouter(l2, l3)
    result = router.filter(
        summary="s",
        action="ls",
        l1_verdict="DANGEROUS",
        l1_reason="命中危险命令",
    )
    print("      状态={}  L3 调用次数={}  不可用次数={}".format(
        result.status, len(l3.calls), router.unavailable_calls
    ))
    print("      契约载荷={}".format(
        json.dumps(result.l2.payload, ensure_ascii=False)
    ))
    report["unavailable"] = {
        "status": result.status,
        "l3_calls": len(l3.calls),
        "unavailable_calls": router.unavailable_calls,
        "payload": result.l2.payload,
    }
    return report


# ===========================================================================
# 演示 3b：真引擎里的升级行
# ===========================================================================
def demo_engine_escalation(work: str) -> Dict[str, Any]:
    """用**真的**引擎跑一遍，看升级行与统计行。

    :param work: 工作目录。
    :return: 证据字典。
    """
    from src.adapters.mcode import McodeAdapter
    from src.core.engine import SentinelEngine
    from src.llm.cascade import CascadeRouter
    from src.rules.basic import RuleEngine

    l2 = _CapturingProvider(['{"verdict":"DRIFT","reason":"跑偏了"}'])
    l3 = _CapturingProvider(['{"verdict":"DANGEROUS","reason":"确实危险"}'])
    router = CascadeRouter(l2, l3)

    log_path = os.path.join(work, "engine-escalate.jsonl")
    _write_jsonl(
        log_path,
        [
            '{"type":"user_message","cwd":"D:/demo","content":"清理构建产物"}',
            '{"type":"tool_call","cwd":"D:/demo","tool_call":'
            '{"name":"Bash","arguments":{"command":"rm -rf /"}}}',
        ],
    )
    alert_log = os.path.join(work, "engine-escalate-alert.log")
    if os.path.exists(alert_log):
        os.remove(alert_log)

    engine = SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=alert_log,
        console=False,
    )
    engine.set_cascade(router)
    try:
        summary = engine.run(log_path)
    finally:
        engine.close()

    print("      引擎判定：{}".format(json.dumps(
        summary.get("counts", {}), ensure_ascii=False
    )))

    content = ""
    if os.path.exists(alert_log):
        with open(alert_log, "r", encoding="utf-8") as handle:
            content = handle.read()

    update_lines = [
        line for line in content.splitlines()
        if "ALERT_UPDATE_JSON" in line
    ]
    cascade_lines = [
        line for line in content.splitlines() if "CASCADE_JSON" in line
    ]
    print("\n──── 真引擎的级联升级行 ────")
    for line in update_lines:
        print("  " + line[:220])
    print("──── 真引擎的级联统计行 ────")
    for line in cascade_lines:
        print("  " + line[:220])

    return {
        "alert_log": alert_log,
        "update_lines": update_lines,
        "cascade_lines": cascade_lines,
        "l3_calls": len(l3.calls),
    }


def main(argv=None) -> int:
    """命令行入口。

    :param argv: 参数列表。
    :return: 进程退出码。
    """
    _prepare_stdout()
    parser = argparse.ArgumentParser(
        description="v3.5.0 多流隔离与级联审核验收演示"
    )
    parser.add_argument("--port", type=int, default=8150, help="看板端口")
    args = parser.parse_args(argv)

    work = os.path.join(ROOT, "logs", "v350-demo")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    evidence: Dict[str, Any] = {"work": work}

    print("=" * 68)
    print("演示 1 —— 两路拆分（挂两个不同工作区的 Agent）")
    print("=" * 68)
    evidence["streams"] = demo_streams(int(args.port), work)

    print("")
    print("=" * 68)
    print("演示 2 —— 极简 L2 Prompt（静态前缀吃满 Prefix Cache）")
    print("=" * 68)
    evidence["prompt"] = demo_prompt()

    print("")
    print("=" * 68)
    print("演示 3 —— L2 → L3 触发（Mock）")
    print("=" * 68)
    evidence["cascade"] = demo_cascade()
    evidence["engine"] = demo_engine_escalation(work)

    out = os.path.join(
        ROOT,
        "_管理文档",
        "文档与报告",
        "assets",
        "v350-multistream-cascade-evidence.json",
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
    print("")
    print("证据 → {}".format(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
