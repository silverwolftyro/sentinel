"""v3.7.0 验收演示 —— 规则管理页、流水线控制阀与极限摘要。

它做的事**每一步都是真的**
==========================

起**真的**看板进程（``src.gui.server``）+ **真的**浏览器，
用 CDP 的**真实鼠标事件**点真实控件：

* **规则管理页** —— 表格里逐条列出 23 条规则，
  用真鼠标点掉一条规则的 **Toggle**，
  再点「保存并应用」，然后回读 ``GET /api/rules``
  确认那条规则的 ``enabled`` 真的变成 ``false``；
* **流水线控制页** —— L1 / L2 / L3 三个独立开关、
  L2→L3 桥接开关、L1 拦截策略下拉、两个 0-100 滑条；
* **看板 100 档极限摘要** —— 往被监控日志里追加
  **连续 5 次读同一个文件**，确认右侧日志真的合并成
  ``[读取] core/auth.py 等 5 个文件``。

⚠️ **桥接是端到端真跑的** ——
   本脚本起一个**本地 Mock L3**（OpenAI 兼容端点，127.0.0.1），
   把演示配置指向它，然后：
   L2 关掉 + L3 打开 + 桥接打开 ⇒ L1 没命中的事件
   **真的**被送进了那个 Mock L3。
   Mock 记录收到几次请求、请求里带了什么 ——
   **L2 一次都不会被调用**（它根本没装配）。

⚠️ 演示用的配置文件写在 ``logs/v370-demo/`` 下，
   **不会**写回 ``config/config.yaml``；
   挂载持久化（``attached_sessions.json``）也落在同一个目录里，
   **绝不**碰用户真实的 ``config/attached_sessions.json``。
⚠️ 演示目录名**不能**命中雷达的默认排除词
   （``sentinel`` / ``stress`` / ``test``）。

用法::

    python _构建与发布/scripts/demo_v370_rules_and_summary.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import yaml

#: 仓库根目录。
ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

#: 看板端口（跟 v3.6.1 演示的 8154 错开，免得撞车）。
PORT = 8155

#: CDP 调试端口。
CDP_PORT = 9365

#: 本地 Mock L3 的端口。
MOCK_L3_PORT = 8765

#: 演示工作目录。
WORK = os.path.join(ROOT, "logs", "v370-demo")

#: 雷达扫描根 —— 指向一个**空目录**，演示不去碰任何真实会话。
RADAR_ROOT = os.path.join(WORK, "radar-root")

#: 被监控的演示日志（mcode 扁平协议）。
WATCH_LOG = os.path.join(WORK, "mcode.jsonl")

#: 演示专用配置。
CONFIG = os.path.join(WORK, "v370-config.yaml")

#: 截图与证据的落点。
ASSETS = os.path.join(ROOT, "_管理文档", "文档与报告", "assets")

#: 候选浏览器。
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

#: 演示里要关掉的那条规则（提权）。
DEMO_RULE_ID = "dangerous.sudo"

#: 演示里连续读的文件（5 次 → 必须合并成一行）。
DEMO_TARGET = "core/auth.py"


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


def _get_json(url: str) -> Dict[str, Any]:
    """读一个 JSON 接口。

    :param url: 地址。
    :return: 解析后的字典。
    """
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


# ===========================================================================
# 本地 Mock L3（OpenAI 兼容端点）
# ===========================================================================
class _MockL3:
    """本地 Mock L3 —— 记录每次被问到什么。

    ⚠️ 它**只在本机回环**上跑，返回的是「看起来正常」的结论；
       它的作用是**证明桥接通路真的通**，
       不是模拟「模型很聪明」。
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, payload: Dict[str, Any]) -> None:
        """记一次请求。"""
        with self._lock:
            self.calls.append(payload)

    def count(self) -> int:
        """被问了几次。"""
        with self._lock:
            return len(self.calls)

    def bodies(self) -> List[str]:
        """把请求体里的用户提示摘出来（证明送过去的是**什么**）。"""
        with self._lock:
            out: List[str] = []
            for item in self.calls:
                messages = item.get("messages") or []
                for message in messages:
                    if message.get("role") == "user":
                        out.append(str(message.get("content") or ""))
                        break
            return out


def _start_mock_l3(mock: _MockL3) -> ThreadingHTTPServer:
    """起一个本地 OpenAI 兼容端点。

    :param mock: 记录用的假 L3。
    :return: 已经在跑的服务对象。
    """

    class Handler(BaseHTTPRequestHandler):
        """只认 ``POST /v1/chat/completions``。"""

        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # noqa: ARG002
            """别把请求打到控制台 —— 演示输出要干净。"""

        def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
            """回一个能被级联解析器认出来的三值判定。"""
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            mock.record(payload)
            verdict = {
                "verdict": "ON_TRACK",
                "reason": "本地 Mock L3：这段动作看起来正常",
            }
            body = json.dumps({
                "id": "mock-l3",
                "object": "chat.completion",
                "created": 0,
                "model": "mock-l3",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(verdict, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(
        ("127.0.0.1", MOCK_L3_PORT), Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ===========================================================================
# 演示素材
# ===========================================================================
def _tool_line(name: str, args: Dict[str, Any]) -> str:
    """造一条工具调用日志行。

    ⚠️ 形态必须是 **mcode 旧协议**（``tool_call`` 为嵌套对象、``arguments``
       为字典）—— 看板前端与 ``src/adapters/mcode.py`` 都按这个契约解析，
       写成 ``{"tool": {...}}`` 或把 ``arguments`` 序列化成字符串都解析不出
       工具名与目标，极限摘要就合并不了。

    :param name: 工具名。
    :param args: 工具参数。
    :return: 一行 JSON。
    """
    return json.dumps({
        "type": "tool_call",
        "tool_call": {"name": name, "arguments": args},
    }, ensure_ascii=False)


def _demo_lines() -> List[str]:
    """演示用的日志行。

    ⚠️ 顺序是**刻意**的 —— 5 次读必须**相邻**，
       中间插一行别的动作就会把合并打断。

    :return: 日志行列表。
    """
    lines = [
        json.dumps({
            "type": "user_message",
            "content": "帮我确认一下登录模块的边界条件。",
        }, ensure_ascii=False),
    ]
    lines.extend(
        _tool_line("Read", {"file_path": DEMO_TARGET})
        for _ in range(5)
    )
    lines.append(_tool_line("Bash", {"command": "pytest -q tests/"}))
    lines.append(_tool_line("Read", {"file_path": "core/users.py"}))
    lines.append(json.dumps({
        "type": "text",
        "text": "看完了，边界条件没问题。",
    }, ensure_ascii=False))
    return lines


def _prepare_watch_log(path: str) -> str:
    """先把被监控日志**建成空文件**。

    ⚠️ 看板的常驻监听是**从文件末尾开始**跟的 ——
       启动前写好的内容它看不到，也就不会过规则、不出流水。

    :param path: 目标路径。
    :return: 路径字符串。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8"):
        pass
    return path


def _append_watch_log(path: str) -> int:
    """往被监控日志里**真的追加**演示事件。

    :param path: 日志路径。
    :return: 追加的行数。
    """
    lines = _demo_lines()
    body = "\n".join(lines)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(body + "\n")
        handle.flush()
    return len(lines)


def _write_demo_config(path: str, scan_root: str) -> str:
    """写一份演示专用配置。

    ⚠️ 用 ``config/config.example.yaml`` 当底稿，
       别的配置项（规则、阈值、模型层）**一个都不动**；
       这一份只改四件事：

       1. 雷达的扫描根 → 一个**空目录**（演示不去碰真实会话）；
       2. 通知通道 → **全部关掉**（演示不该弹窗、不该发 webhook）；
       3. ``presentation`` → 日志 100 档（极限摘要）+ 流水 95 档；
       4. 级联 → **L2 关 / L3 开 / 桥接开**，L3 指向**本地 Mock**。

    :param path: 输出路径。
    :param scan_root: 雷达要扫的目录。
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

    alert = raw.get("alert")
    if not isinstance(alert, dict):
        alert = {}
        raw["alert"] = alert
    alert["webhook_startup_test"] = False
    channels = alert.get("channels")
    if isinstance(channels, list):
        for channel in channels:
            if isinstance(channel, dict):
                channel["enabled"] = False
    files = alert.get("file")
    if isinstance(files, dict):
        files["path"] = os.path.join(WORK, "alerts.jsonl")

    raw["presentation"] = {
        "log_compression": 100,
        "audit_compression": 95,
    }

    llm = raw.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        raw["llm"] = llm
    cascade = llm.get("cascade")
    if not isinstance(cascade, dict):
        cascade = {}
        llm["cascade"] = cascade
    cascade["enabled"] = True
    cascade["l2"] = {"enabled": False}
    cascade["l3"] = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:{}/v1".format(MOCK_L3_PORT),
        "model": "mock-l3",
        "api_key_env": "",
        "timeout": 10,
        "max_tokens": 256,
        "temperature": 0,
        "json_mode": False,
    }
    cascade["bridge_l1_to_l3"] = True

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(raw, handle, allow_unicode=True, sort_keys=False)
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
        #: 收到的 CDP **事件**（不是回包）—— 用来抓页面里的异常。
        self._events: List[dict] = []

    def events(self) -> List[dict]:
        """返回收到的 CDP 事件列表。

        :return: 事件字典列表。
        """
        return list(self._events)

    def exceptions(self) -> List[str]:
        """返回页面里抛出的**异常**摘要。

        :return: 每条一行可读文本。
        """
        lines: List[str] = []
        for event in self._events:
            if event.get("method") != "Runtime.exceptionThrown":
                continue
            detail = (
                event.get("params", {}).get("exceptionDetails", {}) or {}
            )
            text = detail.get("text", "")
            desc = detail.get("exception", {}).get("description", "")
            lines.append("{} | {}".format(text, desc)[:400])
        return lines

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
            if message.get("method"):
                self._events.append(message)

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

    def click(self, selector: str) -> bool:
        """用**真实鼠标事件**点一个元素。

        ⚠️ 必须用真事件 —— 复选框靠的是浏览器的默认切换行为。
        ⚠️ 也必须**先滚到可见位置** ——
           规则表有 23 行，目标行往往在首屏之外；
           视口外的坐标点下去什么都不会发生。

        :param selector: CSS 选择器。
        :return: 点到了返回 ``True``。
        """
        found = self.evaluate(
            "(function(){var n=document.querySelector("
            + json.dumps(selector)
            + ");if(!n){return null;}"
            "if(n.scrollIntoView){n.scrollIntoView({block:'center'});}"
            "return true;})()"
        )
        if not found:
            return False
        time.sleep(0.4)
        box = self.evaluate(
            "(function(){var n=document.querySelector("
            + json.dumps(selector)
            + ");if(!n){return null;}"
            "var r=n.getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2,"
            "top:r.top,bottom:r.bottom};})()"
        )
        if not box:
            return False
        x = float(box["x"])
        y = float(box["y"])
        viewport = self.evaluate(
            "(function(){return {h:window.innerHeight,w:window.innerWidth};})()"
        ) or {}
        height = float(viewport.get("h") or 0)
        if height and not (0 <= y <= height):
            print("     ⚠️ 目标不在视口内（y={:.0f} / 视口高 {:.0f}），跳过".format(
                y, height
            ))
            return False
        self.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "buttons": 1, "clickCount": 1,
        })
        self.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "buttons": 0, "clickCount": 1,
        })
        return True

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


def _goto(cdp: _Cdp, url: str, wait: float = 4.0) -> None:
    """跳到某个页面并等它渲染。

    :param cdp: CDP 客户端。
    :param url: 目标地址。
    :param wait: 等多少秒。
    """
    cdp.send("Page.navigate", {"url": url})
    time.sleep(wait)


# ===========================================================================
# 演示一：规则管理页的 Toggle
# ===========================================================================
def _demo_rules_page(
    cdp: _Cdp, base: str, evidence: Dict[str, Any]
) -> None:
    """在规则管理页上**真的点一次 Toggle**。

    :param cdp: CDP 客户端。
    :param base: 看板地址前缀。
    :param evidence: 证据字典（原地补充）。
    """
    print("     打开 /settings/rules …")
    _goto(cdp, base + "/settings/rules", wait=5.0)

    before = cdp.evaluate(
        "(function(){var n=document.querySelector("
        + json.dumps(
            'tr[data-rule-id="' + DEMO_RULE_ID + '"] input[type=checkbox]'
        )
        + ");return n?{checked:n.checked}:null;})()"
    )
    count = cdp.evaluate(
        "(function(){var n=document.getElementById('rules-table-count');"
        "return n?n.textContent:'';})()"
    )
    print("     规则清单共 {} 条；{} 初始状态 {}".format(
        count, DEMO_RULE_ID, before
    ))

    print("     真鼠标点掉「{}」的 Toggle …".format(DEMO_RULE_ID))
    clicked = cdp.click(
        'tr[data-rule-id="' + DEMO_RULE_ID + '"] .settings-toggle__track'
    )
    time.sleep(1.2)
    after = None
    for _ in range(10):
        after = cdp.evaluate(
            "(function(){var n=document.querySelector("
            + json.dumps(
                'tr[data-rule-id="' + DEMO_RULE_ID + '"] input[type=checkbox]'
            )
            + ");"
            "var s=document.querySelector("
            + json.dumps(
                'tr[data-rule-id="' + DEMO_RULE_ID + '"] select'
            )
            + ");"
            "var body=document.getElementById('rules-table-body');"
            "var ids=[];"
            "if(body){for(var i=0;i<body.children.length;i+=1){"
            "var r=body.children[i];"
            "ids.push(r.getAttribute('data-rule-id')||(':'+r.className));}}"
            "return {found:!!n,"
            "checked:n?n.checked:null,"
            "actionDisabled:s?(s.disabled?true:false):null,"
            "rowCount:body?body.children.length:null,"
            "ids:ids.slice(0,8),"
            "countText:(document.getElementById('rules-table-count')||{}).textContent,"
            "bodyHead:body?body.innerHTML.slice(0,300):null};})()"
        )
        if isinstance(after, dict) and after.get("found"):
            break
        time.sleep(0.5)
    print("     点完之后：{}".format(after))
    for line in cdp.exceptions():
        print("     ⚠️ 页面异常：{}".format(line))

    print("     点「保存并应用」…")
    cdp.click("#save-button")
    time.sleep(4.0)
    saved = _get_json(base + "/api/rules")
    row = None
    for item in saved.get("rules") or []:
        if item.get("id") == DEMO_RULE_ID:
            row = item
            break
    print("     落盘后服务端读到：enabled={} action={}".format(
        (row or {}).get("enabled"), (row or {}).get("action")
    ))

    # ⚠️ 截图前必须**把目标行滚回视野** ——
    #    表格被重画（renderTable）时滚动位置会归零，
    #    直接截图会拍不到那条刚被关掉的规则。
    cdp.evaluate(
        "(function(){var n=document.querySelector("
        + json.dumps('tr[data-rule-id="' + DEMO_RULE_ID + '"]')
        + ");"
        "if(n&&n.scrollIntoView){n.scrollIntoView({block:'center'});}"
        "return true;})()"
    )
    time.sleep(0.6)

    cdp.screenshot(os.path.join(ASSETS, "v370-rules-toggle.png"))

    evidence["rules_page"] = {
        "rule_count": count,
        "rule_id": DEMO_RULE_ID,
        "toggle_clicked": bool(clicked),
        "before": before,
        "after": after,
        "saved_enabled": (row or {}).get("enabled"),
        "saved_action": (row or {}).get("action"),
        "in_disabled_list": DEMO_RULE_ID in (saved.get("disabled") or []),
    }


# ===========================================================================
# 演示二：流水线控制页
# ===========================================================================
def _demo_pipeline_page(
    cdp: _Cdp, base: str, evidence: Dict[str, Any]
) -> None:
    """拍流水线控制页与呈现与摘要卡片。

    :param cdp: CDP 客户端。
    :param base: 看板地址前缀。
    :param evidence: 证据字典（原地补充）。
    """
    print("     打开 /settings/pipeline …")
    _goto(cdp, base + "/settings/pipeline", wait=5.0)
    snapshot = cdp.evaluate(
        "(function(){"
        "function ck(id){var n=document.getElementById(id);"
        "return n?n.checked:null;}"
        "function val(id){var n=document.getElementById(id);"
        "return n?n.value:null;}"
        "var p=document.getElementById('pipeline-path');"
        "return {l1:ck('pipeline-l1-enabled'),"
        "cascade:ck('pipeline-cascade-enabled'),"
        "l2:ck('pipeline-l2-enabled'),"
        "l3:ck('pipeline-l3-enabled'),"
        "bridge:ck('pipeline-bridge-l1-to-l3'),"
        "l1Action:val('pipeline-l1-action'),"
        "logCompression:val('presentation-log-compression'),"
        "auditCompression:val('presentation-audit-compression'),"
        "path:p?p.textContent:''};})()"
    )
    print("     页面读到的开关：{}".format(snapshot))
    cdp.screenshot(os.path.join(ASSETS, "v370-pipeline-card.png"))
    evidence["pipeline_page"] = snapshot


# ===========================================================================
# 演示三：看板 100 档极限摘要
# ===========================================================================
def _demo_extreme_summary(
    cdp: _Cdp, base: str, evidence: Dict[str, Any]
) -> None:
    """看板右侧日志在 100 档下**真的合并**连续动作。

    :param cdp: CDP 客户端。
    :param base: 看板地址前缀。
    :param evidence: 证据字典（原地补充）。
    """
    print("     打开看板 …")
    _goto(cdp, base + "/", wait=6.0)
    print("     往被监控日志追加 {} 行演示事件 …".format(
        _append_watch_log(WATCH_LOG)
    ))
    time.sleep(6.0)

    merged = cdp.evaluate(
        "(function(){var out=[];"
        "var nodes=document.querySelectorAll('.log-line--action');"
        "for(var i=0;i<nodes.length;i+=1){"
        "var b=nodes[i].querySelector('.log-line__body');"
        "out.push(b?b.textContent:'');}"
        "return out;})()"
    ) or []
    print("     100 档下合并出来的行：{}".format(merged))
    cdp.screenshot(os.path.join(ASSETS, "v370-log-extreme-100.png"))

    presentation = _get_json(base + "/api/presentation")
    evidence["extreme_summary"] = {
        "presentation": presentation,
        "merged_rows": merged,
        "merged_count": len(merged),
        "five_reads_merged": any(
            "等 5 个文件" in str(text) for text in merged
        ),
    }


# ===========================================================================
# 演示四：关掉 L2 之后 L1 放行的事件直发 L3
# ===========================================================================
def _demo_bridge(mock: _MockL3, evidence: Dict[str, Any]) -> None:
    """核对桥接到底有没有走通。

    :param mock: 本地 Mock L3。
    :param evidence: 证据字典（原地补充）。
    """
    bodies = mock.bodies()
    print("     Mock L3 被调用 {} 次".format(mock.count()))
    for index, body in enumerate(bodies):
        head = " ".join(str(body).split())[:110]
        print("       #{} {}".format(index + 1, head))
    evidence["bridge"] = {
        "mock_l3_calls": mock.count(),
        "mock_l3_bodies": bodies,
        "l2_assembled": False,
        "l2_calls": 0,
        "l2_calls_note": (
            "L2 **根本没有装配**（l2.enabled=false ⇒ factory 不构建 L2 "
            "provider），所以不存在可观测的 L2 请求 —— 这一栏是**推断**，"
            "不是计数器。真正的证据是请求体里那行"
            "「L2 已关闭（由 L3 直接复核）」。"
        ),
    }


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
    os.makedirs(RADAR_ROOT, exist_ok=True)
    os.makedirs(ASSETS, exist_ok=True)
    if os.path.exists(WATCH_LOG):
        os.remove(WATCH_LOG)
    persist = os.path.join(WORK, "attached_sessions.json")
    if os.path.exists(persist):
        os.remove(persist)

    _prepare_watch_log(WATCH_LOG)
    config = _write_demo_config(CONFIG, RADAR_ROOT)

    mock = _MockL3()
    mock_server = _start_mock_l3(mock)

    evidence: Dict[str, Any] = {
        "version": "v3.7.0",
        "port": PORT,
        "mock_l3": "http://127.0.0.1:{}/v1".format(MOCK_L3_PORT),
        "demo_log": os.path.basename(WATCH_LOG),
        "radar_root": RADAR_ROOT,
    }

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server_log = open(
        os.path.join(WORK, "server.log"), "w", encoding="utf-8"
    )
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--watch", WATCH_LOG,
            "--tail",
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
    browser_proc = None
    cdp = None
    profile = None
    try:
        print("[1/6] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/6] 打开浏览器 …")
        browser = _find_browser()
        profile = os.path.join(
            os.environ.get("TEMP", "."),
            "sentinel-v370-cdp-{}".format(int(time.time() * 1000)),
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
            "deviceScaleFactor": 1,
            "mobile": False,
        })

        print("[3/6] 规则管理页：真点一次 Toggle …")
        _demo_rules_page(cdp, base, evidence)

        print("[4/6] 流水线控制页 …")
        _demo_pipeline_page(cdp, base, evidence)

        print("[5/6] 看板 100 档极限摘要 …")
        _demo_extreme_summary(cdp, base, evidence)

        print("[6/6] 核对桥接 …")
        time.sleep(3.0)
        _demo_bridge(mock, evidence)

        report = os.path.join(ASSETS, "v370-evidence.json")
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("     证据 → {}".format(report))

        rules = evidence.get("rules_page", {})
        pipeline = evidence.get("pipeline_page", {})
        extreme = evidence.get("extreme_summary", {})
        bridge = evidence.get("bridge", {})

        checks = [
            (
                "规则清单 23 条",
                str(rules.get("rule_count")) == "23",
            ),
            (
                "Toggle 点得动（开→关）",
                bool(rules.get("before"))
                and bool(rules.get("before", {}).get("checked"))
                and rules.get("after", {}).get("checked") is False,
            ),
            (
                "关掉后动作选择器一起禁用",
                rules.get("after", {}).get("actionDisabled") is True,
            ),
            (
                "保存后服务端真的关了它",
                rules.get("saved_enabled") is False
                and rules.get("in_disabled_list") is True,
            ),
            (
                "流水线页 L2 关 / L3 开",
                pipeline.get("l2") is False
                and pipeline.get("l3") is True,
            ),
            (
                "流水线页桥接开关打开",
                pipeline.get("bridge") is True,
            ),
            (
                "呈现档位 100 / 95",
                str(pipeline.get("logCompression")) == "100"
                and str(pipeline.get("auditCompression")) == "95",
            ),
            (
                "服务端档位就是 100",
                extreme.get("presentation", {}).get("log_compression") == 100,
            ),
            (
                "100 档真的合并了 5 次读",
                extreme.get("five_reads_merged") is True,
            ),
            (
                "Mock L3 真的被问到了",
                int(bridge.get("mock_l3_calls") or 0) > 0,
            ),
        ]
        failed = [name for name, ok in checks if not ok]
        print("")
        print("验收结论：{}".format(
            "全部通过 ✓" if not failed else "有项目没通过 ✗"
        ))
        for name, ok in checks:
            print("  {:26s} → {}".format(name, "✓" if ok else "✗"))
        if failed:
            print("\n没通过：{}".format("、".join(failed)))
        return 0 if not failed else 1
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except OSError:
                pass
        if browser_proc is not None:
            browser_proc.terminate()
            try:
                browser_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser_proc.kill()
        if profile and os.path.isdir(profile):
            shutil.rmtree(profile, ignore_errors=True)
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        server_log.close()
        mock_server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
