"""v3.5.2 验收演示 —— 流水与告警彻底分离 · 任务锚点自动拔取。

它做的事**每一步都是真的**
==========================

1. **中间面板上下分离（不再重叠）** ——
   起**真的**看板（``src.gui.server``），让**真的**规则引擎判定出
   告警（死循环 + 红线命令），中间面板因此**真的**同时有告警卡片和流水行；
   然后用 CDP 量**真实的几何**：
   告警区底边必须**不高于**流水区顶边（重叠量 ≤ 0），
   流水区的 ``position`` 必须是 ``static``，中间面板里不许有 z-index。
   —— 截图证明「文字不再叠在一起」。

2. **雷达自动拔取任务锚点** ——
   雷达根指向一个**真的**会话目录（``mcode/…/messages.jsonl``），
   它的**第 1 行是系统噪声**（``session_identity_reminder``），
   **第 2 行才是用户真正说过的话**；
   哨兵必须**跳过噪声**、取到第 2 行，
   并在雷达面板上把这条锚点连同「[自动提取]」标记显示出来。

3. **挂载后开启漂移监控** ——
   点「接入」把这份日志挂进来，服务端**自己**重新推导锚点
   （不信任客户端输入），``/api/sessions`` 里必须出现
   ``goal_source == "auto"`` / ``auto_anchored == true``；
   接着往日志里追加**明显跑偏**的事件，
   意图漂移追踪器（窗口 5、连续 2 次）应当**真的**报出 DRIFT 告警。

⚠️ 演示用的配置文件写在 ``logs/v352-demo/`` 下，
   **不会**写回 ``config/config.yaml``；
   它只改了一件事：雷达的扫描根。
⚠️ 挂载用的日志是 ``tail=True`` 跟随 ——
   **必须先挂载、后追加**，否则追加的行不会被读到。

⚠️ 演示环境有**两个真实的坑**，这个脚本必须先绕过去，
   否则取证的根本不是用户能看到的那个界面：

1. **挂载持久化（v3.1.0）** ——
   上一轮演示挂过的会话会被**自动接回来**，
   而雷达**不列已经挂载的文件** ——
   那样雷达面板永远是空的。演示前必须清掉
   ``logs/v352-demo/attached_sessions.json``。
2. **雷达只列「正在写入」的会话** ——
   演示日志要是静静躺着，就会被当成历史会话收进「显示其余」。
   所以脚本会**真写**一条心跳（普通 ``thinking``，不含用户消息），
   让它保持「Agent 正在干活」的形态。

用法::

    python _构建与发布/scripts/demo_v352_layout_and_auto_anchor.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import yaml

#: 仓库根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 让 ``import src`` 能用（脚本在 ``_构建与发布/scripts/`` 下）。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 浏览器可执行文件候选路径。
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

#: 看板端口（跟 v3.5.1 的 8151 错开，免得撞车）。
PORT = 8152

#: CDP 调试端口。
CDP_PORT = 9362

#: 演示工作目录。
WORK = os.path.join(ROOT, "logs", "v352-demo")

#: 雷达扫描根（演示专用，里面躺着一份**真的** mcode 会话日志）。
RADAR_ROOT = os.path.join(WORK, "radar-root")

#: 雷达会话日志的路径（``mcode`` 目录段 → 被认成 MCode）。
SESSION_DIR = os.path.join(RADAR_ROOT, "mcode", "2026", "10", "30", "14-31-44-demo")
SESSION_PATH = os.path.join(SESSION_DIR, "messages.jsonl")

#: 用户真正说过的那句话 —— 哨兵**应该**把它拔成任务锚点。
USER_GOAL = "请帮我把 src/core/auth.py 的登录逻辑重构一遍，并补上对应的单元测试。"

#: 第 1 行的系统噪声 —— 哨兵**不应该**把它当成任务锚点。
NOISE_LINE = {
    "message": {
        "role": "custom",
        "customType": "session_identity_reminder",
        "content": "<system-reminder>\nYOUR SESSION ID: 7f3a91c4\n</system-reminder>",
    }
}

#: 第 2 行 —— 用户真正说的话。
USER_LINE = {
    "message": {
        "role": "user",
        "content": [{"type": "text", "text": USER_GOAL}],
    }
}


# ===========================================================================
# 小工具
# ===========================================================================
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


def _find_browser() -> str:
    """找一个可用的 Chromium 内核浏览器。

    :return: 浏览器可执行文件路径。
    :raises FileNotFoundError: 一个都没找到时抛出。
    """
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("找不到 Edge / Chrome")


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


def _get_json(url: str, timeout: float = 60.0) -> Dict[str, Any]:
    """GET 一个 JSON。

    :param url: 地址。
    :param timeout: 超时（秒）。
    :return: 解析出来的字典。
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _post_json(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """POST 一个 JSON。

    :param url: 地址。
    :param payload: 请求体。
    :param timeout: 超时（秒）。
    :return: 解析出来的字典。
    """
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ===========================================================================
# 演示素材
# ===========================================================================
def _write_watch_log(path: str) -> str:
    """写一份**真的** MCode 格式演示日志（给中间面板出告警 + 流水）。

    ⚠️ 三段内容是**故意**这么排的：

    * 正常的 ``thinking`` 与工具调用 → 引擎判 ``ON_TRACK``
      → 流水区出现**绿色**的行；
    * **连续三次**读同一个文件 → 死循环检测器命中 → ``LOOP``
      → 出现**橘黄**的告警；
    * 一条 ``rm -rf /`` → 红线级 → ``DANGEROUS`` → 出现**红色**的告警。

    ⚠️ **不放** ``user_message`` ——
    放了会被自动锚定接管，顶部「任务目标」会多出一行，干扰画面。

    :param path: 目标路径。
    :return: 路径字符串。
    """
    lines: List[Dict[str, Any]] = [
        {"type": "thinking", "text": "先确认这个模块的边界条件，再决定改不改签名。"},
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/engine.py"},
            },
        },
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Bash",
                "arguments": {"command": "python -m pytest -q tests/"},
            },
        },
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/auth.py"},
            },
        },
        # ↓ 下面两行紧挨着 —— 死循环检测器要的是「连续」。
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/auth.py"},
            },
        },
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/auth.py"},
            },
        },
        # ↑ 连续第三次读同一个文件 → LOOP（橘黄告警）。
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Bash",
                "arguments": {"command": "rm -rf /"},
            },
        },
        # ↑ 红线命令 → DANGEROUS（红色告警）。
        {"type": "thinking", "text": "失败的两条都指向同一个文件，先单独复现。"},
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Grep",
                "arguments": {"pattern": "ON_TRACK"},
            },
        },
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/rules/basic.py"},
            },
        },
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    return path


def _write_radar_session(path: str) -> str:
    """写一份**真的** mcode 会话日志（给雷达拔锚点用）。

    ⚠️ **第 1 行故意是系统噪声** ——
    哨兵必须**跳过它**，取到第 2 行用户真正说过的话。
    这一步证明「噪声过滤」不是摆设。

    :param path: 目标路径。
    :return: 路径字符串。
    """
    lines: List[Dict[str, Any]] = [
        NOISE_LINE,
        USER_LINE,
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/auth.py"},
            },
        },
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body + "\n")
    return path


def _append_drift_events(path: str, count: int) -> int:
    """往会话日志里追加**明显跑偏**的事件。

    ⚠️ 挂载是 ``tail=True`` 跟随 —— **必须挂载之后才追加**，
    否则这些行根本不会被读到。

    目标说的是「重构 auth 登录逻辑 + 补测试」，
    这里干的是「改前端 CSS 配色」——
    差得够远，模型才判得出「跑偏」。

    :param path: 日志路径。
    :param count: 追加多少个事件。
    :return: 实际追加的事件数。
    """
    payload: List[str] = []
    for index in range(count):
        payload.append(json.dumps({
            "type": "tool_call",
            "tool_call": {
                "name": "Edit",
                "arguments": {
                    "file_path": "web/src/styles/theme.css",
                    "old_string": "--primary: #2b6cb0;",
                    "new_string": "--primary: #e53e3e;",
                },
            },
        }, ensure_ascii=False))
        payload.append(json.dumps({
            "type": "thinking",
            "text": "顺手把主题色从蓝色换成红色，再调一下圆角。",
        }, ensure_ascii=False))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(payload) + "\n")
        handle.flush()
    return len(payload)


def _start_radar_heartbeat(
    path: str,
    interval: float = 15.0,
) -> threading.Event:
    """后台往雷达会话日志里追加心跳，让它保持「正在写入」。

    真实场景里 Agent 一直在往自己的日志里写东西 ——
    雷达**只列正在写入的会话**，静默文件收在「显示其余」里。
    演示文件要是静静躺着，雷达就只会把它当历史会话，
    那一栏也就证明不了「自动拔锚点」。

    ⚠️ 心跳是**真写**（不是改 mtime）——
    这样 mtime 自然更新，内容也符合「Agent 正在干活」的形态。
    心跳内容是普通 ``thinking``，**不含**用户消息，
    不会干扰锚点提取（锚点取的是**头部**第一条用户消息）。

    :param path: 会话日志路径。
    :param interval: 心跳间隔（秒）。
    :return: 停止信号；``set()`` 之后线程会退出。
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "type": "thinking",
                        "text": "正在整理上下文……",
                    }, ensure_ascii=False) + "\n")
            except OSError:
                return

    worker = threading.Thread(target=_loop, daemon=True)
    worker.start()
    return stop


def _write_demo_config(path: str, scan_root: str) -> str:
    """写一份演示专用配置：只改**雷达的扫描根**。

    ⚠️ 用的是 ``config/config.example.yaml`` 当底稿，
    别的配置项（规则、阈值、通知通道、模型层）**一个都不动**；
    这份配置只存在于 ``logs/v352-demo/`` 下，
    **不会**写回 ``config/config.yaml``。

    :param path: 输出路径。
    :param scan_root: 雷达要扫的目录（演示里指向那份假会话目录）。
    :return: 输出路径。
    """
    example = os.path.join(ROOT, "config", "config.example.yaml")
    with open(example, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle.read()) or {}
    radar = raw.get("radar")
    if not isinstance(radar, dict):
        radar = {}
        raw["radar"] = radar
    radar["scan_roots"] = [scan_root]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(
            raw, handle, allow_unicode=True, sort_keys=False
        )
    return path


# ===========================================================================
# 极简 CDP 客户端
# ===========================================================================
class _Cdp:
    """极简 CDP 客户端 —— 只够演示用。

    :param ws_url: 目标页面的 WebSocket 地址。
    """

    def __init__(self, ws_url: str) -> None:
        import websocket

        self._ws = websocket.create_connection(ws_url, timeout=60)
        self._counter = 0

    def send(self, method: str, params: Optional[dict] = None) -> dict:
        """发一条 CDP 命令并等它的回包。

        :param method: 命令名。
        :param params: 参数。
        :return: 回包字典。
        """
        self._counter += 1
        self._ws.send(json.dumps({
            "id": self._counter,
            "method": method,
            "params": params or {},
        }))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == self._counter:
                return message

    def evaluate(self, expression: str):
        """在页面里跑一段 JS 并回读它的返回值。

        :param expression: JS 表达式。
        :return: 表达式的值（拿不到就是 ``None``）。
        """
        reply = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        result = reply.get("result", {}).get("result", {})
        if result.get("subtype") == "error":
            return None
        return result.get("value")

    def screenshot(self, path: str) -> str:
        """拍一张整屏图。

        :param path: 输出路径。
        :return: 输出路径。
        """
        shot = self.send("Page.captureScreenshot", {"format": "png"})
        data = shot.get("result", {}).get("data")
        if not data:
            raise RuntimeError("截图失败：CDP 没返回图像数据")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data))
        return path

    def close(self) -> None:
        """关闭连接。"""
        self._ws.close()


def _wait_for(
    cdp: _Cdp,
    expression: str,
    timeout: float = 30.0,
    interval: float = 0.5,
):
    """轮询一个 JS 表达式，直到它返回真值。

    :param cdp: CDP 客户端。
    :param expression: JS 表达式。
    :param timeout: 最长等多少秒。
    :param interval: 轮询间隔（秒）。
    :return: 第一次为真的返回值；超时返回 ``None``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(interval)
    return None


# ===========================================================================
# 演示一：中间面板上下分离
# ===========================================================================
#: 量中间面板几何的 JS。
_JS_GEOMETRY = """
(function(){
  var panel=document.querySelector('.panel--alerts');
  var body=document.querySelector('.panel--alerts .panel__body');
  var audit=document.querySelector('.live-audit');
  var head=document.querySelector('.live-audit__head');
  var card=document.querySelector('.alert-card');
  if(!panel||!body||!audit){return null;}
  var p=panel.getBoundingClientRect();
  var b=body.getBoundingClientRect();
  var a=audit.getBoundingClientRect();
  var c=card?card.getBoundingClientRect():null;
  function cs(node){return node?getComputedStyle(node):null;}
  return {
    panel_display: cs(panel).display,
    panel_direction: cs(panel).flexDirection,
    body_flex: cs(body).flex,
    body_overflow_y: cs(body).overflowY,
    audit_position: cs(audit).position,
    audit_flex: cs(audit).flex,
    audit_height: Math.round(a.height),
    audit_overflow: cs(audit).overflow,
    lines_overflow_y: (function(){
      var l=document.querySelector('.live-audit__lines');
      return l?getComputedStyle(l).overflowY:null;
    })(),
    z_alerts_panel: cs(panel).zIndex,
    z_alerts_body: cs(body).zIndex,
    z_audit: cs(audit).zIndex,
    z_audit_head: cs(head)?cs(head).zIndex:null,
    z_alert_card: cs(card)?cs(card).zIndex:null,
    alerts_rect: {top: Math.round(b.top), bottom: Math.round(b.bottom),
                  height: Math.round(b.height)},
    audit_rect: {top: Math.round(a.top), bottom: Math.round(a.bottom),
                 height: Math.round(a.height)},
    card_bottom: c?Math.round(c.bottom):null,
    overlap_px: Math.round(Math.max(0, b.bottom - a.top)),
    card_overlap_px: c?Math.round(Math.max(0, c.bottom - a.top)):null,
    alert_cards: document.querySelectorAll('.alert-card').length,
    audit_lines: document.querySelectorAll('.live-audit__line').length
  };
})()
"""


def _demo_layout(
    cdp: _Cdp,
    base: str,
    evidence: Dict[str, Any],
) -> None:
    """演示一：中间面板上下分离，**不再重叠**。

    :param cdp: CDP 客户端。
    :param base: 看板地址。
    :param evidence: 证据字典（原样修改）。
    """
    print("[3/7] 触发扫描，让引擎真的判出告警 …")
    try:
        result = _post_json(base + "/api/scan")
        print("      扫描返回：{}".format(
            result.get("ok") if isinstance(result, dict) else result
        ))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("      扫描接口没响应：{}".format(exc))

    print("[4/7] 等告警卡片与流水行都出现 …")
    _wait_for(
        cdp,
        "document.querySelectorAll('.alert-card').length >= 1",
        timeout=40,
    )
    _wait_for(
        cdp,
        "document.querySelectorAll('.live-audit__line').length >= 3",
        timeout=40,
    )
    time.sleep(1.5)

    geometry = cdp.evaluate(_JS_GEOMETRY)
    print("      几何：{}".format(geometry))
    if isinstance(geometry, dict):
        print("      告警区底边 {} / 流水区顶边 {} → 重叠 {} px".format(
            geometry.get("alerts_rect", {}).get("bottom"),
            geometry.get("audit_rect", {}).get("top"),
            geometry.get("overlap_px"),
        ))

    shot = os.path.join(
        ROOT, "_管理文档", "文档与报告", "assets", "v352-layout-split.png"
    )
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    evidence["middle_panel_layout"] = {
        "geometry": geometry,
        "no_overlap": bool(
            isinstance(geometry, dict)
            and int(geometry.get("overlap_px") or 0) <= 0
            and int(geometry.get("card_overlap_px") or 0) <= 0
        ),
        "audit_is_static_flow_row": bool(
            isinstance(geometry, dict)
            and geometry.get("audit_position") == "static"
        ),
        "no_z_index_in_middle_panel": bool(
            isinstance(geometry, dict)
            and (geometry.get("z_alerts_panel") or "auto") == "auto"
            and (geometry.get("z_alerts_body") or "auto") == "auto"
            and (geometry.get("z_audit") or "auto") == "auto"
            and (geometry.get("z_audit_head") or "auto") == "auto"
            and (geometry.get("z_alert_card") or "auto") == "auto"
        ),
        "screenshot": os.path.basename(shot),
    }


# ===========================================================================
# 演示二：雷达自动拔锚点 + 挂载后开启漂移监控
# ===========================================================================
def _demo_auto_anchor(
    cdp: _Cdp,
    base: str,
    evidence: Dict[str, Any],
    heartbeat: threading.Event,
) -> None:
    """演示二：雷达自动拔锚点 → 挂载 → 漂移监控真的开起来。

    :param cdp: CDP 客户端。
    :param base: 看板地址。
    :param evidence: 证据字典（原样修改）。
    :param heartbeat: 会话日志心跳的停止信号。
    """
    print("[5/7] 打开「发现 / 挂载进程」弹窗 …")
    cdp.evaluate(
        "(function(){var b=document.getElementById('attach-button');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    found = _wait_for(
        cdp,
        "!!document.querySelector('.attach__goal-text')",
        timeout=40,
    )
    time.sleep(1.0)

    goal_text = cdp.evaluate(
        "(function(){var n=document.querySelector('.attach__goal-text');"
        "return n?n.textContent:null;})()"
    )
    goal_tag = cdp.evaluate(
        "(function(){var n=document.querySelector('.attach__goal-tag');"
        "return n?n.textContent:null;})()"
    )
    print("      雷达面板上的锚点：{}".format(goal_text))
    print("      标记：{}".format(goal_tag))

    shot = os.path.join(
        ROOT, "_管理文档", "文档与报告", "assets", "v352-auto-anchor.png"
    )
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    api_goal = ""
    api_found = False
    try:
        discovered = _get_json(base + "/api/discover?active_only=0")
        for item in discovered.get("sessions") or []:
            if str(item.get("path") or "") == os.path.abspath(SESSION_PATH):
                api_found = True
                api_goal = str(item.get("task_goal") or "")
                break
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("      雷达接口没响应：{}".format(exc))
    print("      接口回的 task_goal：{}".format(api_goal))

    evidence["auto_anchor"] = {
        "anchor_shown_in_radar": bool(found),
        "anchor_text": goal_text,
        "anchor_tag": goal_tag,
        "anchor_matches_user_words": goal_text == USER_GOAL,
        "api_task_goal": api_goal,
        "api_task_goal_matches": api_goal == USER_GOAL,
        "api_session_found": api_found,
        "screenshot": os.path.basename(shot),
    }

    print("[6/7] 点「接入」，把这份日志挂进来 …")
    # 心跳到此为止 —— 后面要往日志里塞「跑偏」事件，
    # 心跳继续写会把漂移滑窗搅浑。
    heartbeat.set()
    clicked = cdp.evaluate(
        "(function(){var want=" + json.dumps(os.path.abspath(SESSION_PATH)) + ";"
        "var rows=document.querySelectorAll('.attach__item');"
        "for(var i=0;i<rows.length;i++){"
        "  var meta=rows[i].querySelector('.attach__meta');"
        "  var b=rows[i].querySelector('button.btn--primary');"
        "  if(!b||b.disabled){continue;}"
        "  if(meta && (meta.textContent||'').indexOf(want)>=0){"
        "    b.click();return 'clicked-target';}"
        "}"
        "for(var j=0;j<rows.length;j++){"
        "  var b2=rows[j].querySelector('button.btn--primary');"
        "  if(b2&&!b2.disabled){b2.click();return 'clicked-first';}"
        "}"
        "return 'no-button';})()"
    )
    print("      接入按钮：{}".format(clicked))
    attached = None
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            sessions = _get_json(base + "/api/sessions")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        for item in sessions.get("sessions") or []:
            if str(item.get("watch_path") or "") == os.path.abspath(
                SESSION_PATH
            ):
                attached = item
                break
        if attached:
            break
    if attached is None:
        print("      挂载没成功 —— 证据不完整")
        evidence["auto_anchor"]["attached"] = False
        return

    print("      挂上了：goal_source={} auto_anchored={}".format(
        attached.get("goal_source"), attached.get("auto_anchored")
    ))
    print("      任务目标：{}".format(attached.get("goal")))

    evidence["auto_anchor"]["attached"] = True
    evidence["auto_anchor"]["session_goal_source"] = attached.get(
        "goal_source"
    )
    evidence["auto_anchor"]["session_auto_anchored"] = bool(
        attached.get("auto_anchored")
    )
    evidence["auto_anchor"]["session_goal"] = attached.get("goal")

    # ⚠️ 告警面板**只渲染当前活动会话**的卡片 ——
    #    漂移告警属于挂载会话，不切过去就永远看不见它。
    #    这一步不是"摆拍"，是前端本来就有的会话隔离（v1.0.0 起）。
    session_id = str(attached.get("id") or "")
    print("      切到挂载会话：{}".format(session_id))
    switched = cdp.evaluate(
        "(function(){"
        "  var id=" + json.dumps(session_id) + ";"
        "  var items=document.querySelectorAll('.session-item');"
        "  for(var i=0;i<items.length;i++){"
        "    if(items[i].dataset.sessionId===id){"
        "      items[i].click();return 'clicked';}"
        "  }"
        "  return 'not-found';})()"
    )
    print("      切换结果：{}".format(switched))
    active = _wait_for(
        cdp,
        "(function(){"
        "  var id=" + json.dumps(session_id) + ";"
        "  var n=document.querySelector('.session-item--active');"
        "  return n && n.dataset.sessionId===id ? 1 : 0;})()",
        timeout=30,
    )
    print("      活动会话已切换：{}".format(bool(active)))
    auto_tag = cdp.evaluate(
        "(function(){"
        "  var id=" + json.dumps(session_id) + ";"
        "  var items=document.querySelectorAll('.session-item');"
        "  for(var i=0;i<items.length;i++){"
        "    if(items[i].dataset.sessionId===id){"
        "      var t=items[i].querySelector('.session-item__auto');"
        "      return t?t.textContent:null;}"
        "  }"
        "  return null;})()"
    )
    print("      会话卡片上的自动标记：{}".format(auto_tag))
    evidence["auto_anchor"]["session_switched"] = bool(active)
    evidence["auto_anchor"]["session_auto_tag"] = auto_tag

    # 把弹窗关掉 —— 不关的话漂移告警卡片会被遮住，
    # 截图就证明不了「漂移监控真的开着」。
    closed = cdp.evaluate(
        "(function(){var b=document.querySelector("
        "'#sentinel-modal .modal__close');"
        "if(!b){return 'no-close';}b.click();return 'closed';})()"
    )
    print("      关闭雷达弹窗：{}".format(closed))
    time.sleep(1.0)

    print("[7/7] 追加跑偏的事件，等漂移告警真的出来 …")
    appended = _append_drift_events(SESSION_PATH, 12)
    print("      追加了 {} 个事件".format(appended))

    drift = _wait_for(
        cdp,
        "(function(){"
        "  var n=document.querySelectorAll('.alert-card');"
        "  for(var i=0;i<n.length;i++){"
        "    if((n[i].textContent||'').indexOf('意图漂移')>=0){return 1;}"
        "  }"
        "  return 0;})()",
        timeout=300,
        interval=2.0,
    )
    time.sleep(1.5)

    drift_text = cdp.evaluate(
        "(function(){var n=document.querySelectorAll('.alert-card');"
        "for(var i=0;i<n.length;i++){"
        "  if((n[i].textContent||'').indexOf('意图漂移')>=0){"
        "    return n[i].textContent;}"
        "}return null;})()"
    )
    print("      漂移告警：{}".format(drift_text))

    drift_shot = os.path.join(
        ROOT, "_管理文档", "文档与报告", "assets",
        "v352-auto-anchor-drift.png",
    )
    cdp.screenshot(drift_shot)
    print("      截图 → {}".format(drift_shot))

    evidence["auto_anchor"]["drift_events_appended"] = appended
    evidence["auto_anchor"]["drift_alert_found"] = bool(drift)
    evidence["auto_anchor"]["drift_alert_text"] = drift_text
    evidence["auto_anchor"]["drift_screenshot"] = os.path.basename(drift_shot)


# ===========================================================================
# 主流程
# ===========================================================================
def main(argv=None) -> int:
    """命令行入口。

    :param argv: 参数列表（这里不用）。
    :return: 进程退出码。
    """
    _prepare_stdout()
    base = "http://127.0.0.1:{}".format(PORT)
    os.makedirs(WORK, exist_ok=True)
    watch = os.path.join(WORK, "mcode.jsonl")
    alert_log = os.path.join(WORK, "sentinel_alert.log")
    for path in (watch, alert_log, SESSION_PATH):
        if os.path.exists(path):
            os.remove(path)
    # ⚠️ 挂载持久化（v3.1.0）——
    #    上一轮演示挂过的会话会被**自动接回来**，
    #    而雷达**不列已经挂载的文件** ——
    #    那样雷达面板永远是空的，「自动拔锚点」就没法取证。
    #    演示前必须把这份持久化记录清干净。
    persist = os.path.join(WORK, "attached_sessions.json")
    if os.path.exists(persist):
        os.remove(persist)
        print("      清掉上一轮的挂载持久化记录")
    # 挂载会话的告警日志是**按路径确定**的 —— 演示前清干净，
    # 别让上一轮残留的告警混进画面。
    attached_dir = os.path.join(ROOT, "logs", "attached")
    if os.path.isdir(attached_dir):
        for name in os.listdir(attached_dir):
            if name.startswith("messages-"):
                stale = os.path.join(attached_dir, name)
                try:
                    os.remove(stale)
                except OSError as exc:
                    print("      旧告警日志删不掉（不影响演示）：{}".format(exc))

    _write_watch_log(watch)
    _write_radar_session(SESSION_PATH)
    # 让这份会话日志保持「正在写入」—— 雷达只列活跃会话。
    heartbeat = _start_radar_heartbeat(SESSION_PATH)
    config = _write_demo_config(
        os.path.join(WORK, "v352-config.yaml"), RADAR_ROOT
    )

    evidence: Dict[str, Any] = {
        "version": "v3.5.2",
        "port": PORT,
        "demo_log": os.path.basename(watch),
        "radar_session": SESSION_PATH,
        "expected_goal": USER_GOAL,
    }

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server_log = open(
        os.path.join(WORK, "server.log"), "w", encoding="utf-8"
    )
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--watch", watch,
            "--alert-log", alert_log,
            "--config", config,
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--no-browser",
            "--no-health",
            "--no-guardian",
        ],
        cwd=ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    browser = None
    browser_proc = None
    cdp = None
    try:
        print("[1/7] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/7] 打开浏览器 …")
        browser = _find_browser()
        profile = os.path.join(
            os.environ.get("TEMP", "."),
            "sentinel-v352-cdp-{}".format(int(time.time() * 1000)),
        )
        browser_proc = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--remote-debugging-port={}".format(CDP_PORT),
                "--user-data-dir={}".format(profile),
                "--window-size=1600,1000",
                "--hide-scrollbars",
                "--no-first-run",
                "--disable-gpu",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        target = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:{}/json/list".format(CDP_PORT),
                    timeout=2,
                ) as response:
                    targets = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError):
                continue
            for item in targets:
                if item.get("type") == "page":
                    target = item
                    break
            if target:
                break
        if not target:
            print("      连不上 DevTools —— 证据不完整")
            return 1
        cdp = _Cdp(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": 1600,
            "height": 1000,
            "deviceScaleFactor": 2,
            "mobile": False,
        })
        cdp.send("Page.navigate", {"url": base + "/"})
        time.sleep(5.0)

        _demo_layout(cdp, base, evidence)
        _demo_auto_anchor(cdp, base, evidence, heartbeat)

        print("     写证据 …")
        report = os.path.join(
            ROOT,
            "_管理文档",
            "文档与报告",
            "assets",
            "v352-auto-anchor-evidence.json",
        )
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("     证据 → {}".format(report))

        layout = evidence.get("middle_panel_layout", {})
        anchor = evidence.get("auto_anchor", {})
        ok = (
            layout.get("no_overlap")
            and layout.get("audit_is_static_flow_row")
            and layout.get("no_z_index_in_middle_panel")
            and anchor.get("anchor_shown_in_radar")
            and anchor.get("anchor_matches_user_words")
            and anchor.get("api_task_goal_matches")
            and anchor.get("session_auto_anchored")
            and anchor.get("session_switched")
            and anchor.get("drift_alert_found")
        )
        print("")
        print("验收结论：{}".format("全部通过 ✓" if ok else "有项目没通过 ✗"))
        print("  布局不重叠      → {}".format(layout.get("no_overlap")))
        print("  流水是正常一行  → {}".format(
            layout.get("audit_is_static_flow_row")
        ))
        print("  中间面板无 z-index → {}".format(
            layout.get("no_z_index_in_middle_panel")
        ))
        print("  雷达显示锚点    → {}".format(
            anchor.get("anchor_shown_in_radar")
        ))
        print("  锚点内容正确    → {}".format(
            anchor.get("anchor_matches_user_words")
        ))
        print("  接口 task_goal  → {}".format(
            anchor.get("api_task_goal_matches")
        ))
        print("  挂载后自动锚定  → {}".format(
            anchor.get("session_auto_anchored")
        ))
        print("  已切到挂载会话  → {}".format(
            anchor.get("session_switched")
        ))
        print("  漂移告警真的来了 → {}".format(
            anchor.get("drift_alert_found")
        ))
        return 0 if ok else 1
    finally:
        heartbeat.set()
        if cdp is not None:
            try:
                cdp.close()
            except (OSError, RuntimeError):
                pass
        if browser_proc is not None and browser_proc.poll() is None:
            browser_proc.terminate()
            try:
                browser_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser_proc.kill()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        server_log.close()


if __name__ == "__main__":
    sys.exit(main())
