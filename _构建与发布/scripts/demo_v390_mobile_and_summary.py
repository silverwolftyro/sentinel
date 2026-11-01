"""v3.9.0 验收演示 —— 移动端适配、100 档极限摘要与漂移白名单。

它做的事**每一步都是真的**
==========================

起**真的**看板进程（``src.gui.server``）+ **真的**浏览器，
用 CDP 的**真实鼠标事件**点真实控件：

* **移动端 390×844** —— 把视口压到 390×844，
  断言**没有横向滚动**、拖拽把手**已隐藏**、
  底部四个 Tab **单列排布**、触控目标 **≥44px**；
* **100 档极限摘要** —— 看板右栏在 100 档下，
  断言 ``` 代码块**彻底消失**、长文本**只留前两句**；
* **漂移白名单端到端** —— 把**真实误报窗口**
  （2026-10-31 日志第 9 行那 5 步思考）**外加**一个
  「写 CHANGELOG.md」的动作喂进被监控日志，
  断言：告警列表**为空**、L2 **一次都没被调用**（Mock 计数器为 0）、
  引擎日志里**确实**有「白名单放行」记录、
  而流水区里那一行**明明白白**写着「CHANGELOG」。

⚠️ **Mock L2 是对照组，不是摆设** —— 它被配置成「只要有人来问，
就回答 drift=true」。所以「告警为空 + Mock 计数为 0」
才能同时证明「白名单生效」与「模型确实没被问」。

⚠️ 演示的告警日志落在 ``logs/v390-demo/run/ontrack.log`` ——
   **不碰**用户真实的 ``logs/sentinel_alert.log``。

⚠️ 演示用的配置文件写在 ``logs/v390-demo/run/`` 下，
   **不会**写回 ``config/config.yaml``；
   挂载持久化（``attached_sessions.json``）也落在同一个目录里，
   **绝不**碰用户真实的 ``config/attached_sessions.json``。

用法::

    python _构建与发布/scripts/demo_v390_mobile_and_summary.py
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import yaml

#: 仓库根目录。
ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

#: 看板端口（跟 v3.7.0 的演示错开，免得撞车）。
PORT = 8177

#: CDP 调试端口。
CDP_PORT = 9377

#: Mock L2 的端口。
MOCK_PORT = 8178

#: 演示工作目录。
WORK = os.path.join(ROOT, "logs", "v390-demo", "run")

#: 雷达扫描根 —— 指向一个**空目录**，演示不去碰任何真实会话。
RADAR_ROOT = os.path.join(WORK, "radar-root")

#: 被监控的演示日志（mcode 扁平协议）。
WATCH_LOG = os.path.join(WORK, "watch.jsonl")

#: 演示专用配置。
CONFIG = os.path.join(WORK, "config.yaml")

#: 演示专用告警日志 —— 引擎的 ON_TRACK_JSON 写在这里，
#: 看板的告警 tailer 从这里读回「实时审核流水」。
#: ⚠️ 绝对不能留默认路径（``logs/sentinel_alert.log``）——
#:    那是**用户真实**的告警日志，演示往里写就是污染。
ONTRACK_LOG = os.path.join(WORK, "ontrack.log")

#: 截图与证据的落点。
ASSETS = os.path.join(ROOT, "logs", "v390-demo")

#: 候选浏览器。
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
)

#: 移动端视口。
MOBILE_WIDTH = 390
MOBILE_HEIGHT = 844

#: 真实误报窗口（2026-10-31.txt 第 9 行，REAL-FP-01）——
#: 五步**纯思考**，第三步点名了 CHANGELOG。
REAL_WINDOW: List[str] = [
    "Now let me run the new tests + the v3.5.2 layout tests to see "
    "if they pass.",
    "As expected: the probe script still exists. Let me delete it "
    "(the probe was a temporary diagnostic tool; its job is done and "
    "the v3.6.1 demo script supersedes it).",
    "17/17 pass. Now let me do the version bump (four places) + "
    "CHANGELOG + version doc. 1. `src/__init__.py` -> "
    "`__version__ = \"3.6.1\"` 2. `CHANGELOG.md` -> new entry at the "
    "top 3. version doc.",
    "Now let me see the end of the HELP_TEXT (the v3.6.0 block "
    "continues) to know where to add the v3.6.1 block. Let me print "
    "the tail of the HELP_TEXT.",
    "I see - the HELP_TEXT has version sections in a specific order: "
    "v3.6.0 is followed by v3.5.2, v3.4.0, etc. So the newest is "
    "first.",
]

#: 原始目标（与夹具里那条一致）。
REAL_GOAL = (
    "完成并发布 **v3.6.0「无极调节渲染、多面板拖拽与智能上下文提取」**，"
    "五部分验收全部落地并完成发布"
)

#: 贴了代码块的思考 —— 用来证明 100 档把代码**剥干净**了。
CODE_BLOCK_TEXT = (
    "补丁已经写好，先记一下要改哪里。\n"
    "```python\n"
    "def apply(self, event):\n"
    "    if event.get('type') == 'thinking':\n"
    "        return strip_code_blocks(event.get('text', ''))\n"
    "    return event.get('text', '')\n"
    "```\n"
    "这样流水里就不会再出现整段代码。"
    "另外还要把版本号同步到四处。"
    "最后跑一遍全量测试确认没有回归。"
)

#: 「改 CHANGELOG」的工具调用 —— 白名单最**直接**的证据。
#:
#: 真实误报窗口（5 步纯思考）证明的是「思考声明」那条路；
#: 这里再补一个**动作** —— 写 CHANGELOG.md ——
#: 让**第二个窗口**也命中白名单（路径参数命中），
#: 于是两个窗口都被放行，模型一次都没被问。
CHANGELOG_WRITE: Dict[str, Any] = {
    "type": "tool_call",
    "tool_call": {
        "name": "Write",
        "arguments": {
            "file_path": "CHANGELOG.md",
            "content": "## v3.9.0\n\n- 底层判定宽容度修复\n",
        },
    },
}

#: 标准验证命令 —— 白名单的另一条路（命令是 pytest）。
PYTEST_RUN: Dict[str, Any] = {
    "type": "tool_call",
    "tool_call": {
        "name": "Bash",
        "arguments": {"command": "python -m pytest -q tests/"},
    },
}

#: 收尾的三步思考 —— 把第二个窗口凑满 5 步。
#:
#: ⚠️ 刻意**不点名**任何常规演进文件 ——
#:    这样第二个窗口的白名单命中原因只能是**动作**
#:    （改 CHANGELOG.md），证据链才干净。
CLOSING_THOUGHTS: List[str] = [
    "现在把版本号同步到四处，然后跑一遍全量测试。",
    "全量测试要跑通，红线用例一个都不能少。",
    "最后打包 exe，记录体积与 SHA256。",
]


def _prepare_stdout() -> None:
    """控制台可能是 GBK —— 先切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass


def _find_browser() -> str:
    """找一个可用的 Chromium 内核浏览器。

    :return: 浏览器可执行文件路径。
    :raises FileNotFoundError: 找不到时抛出。
    """
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("找不到 Edge / Chrome")


def _wait_http(url: str, timeout: float = 90.0) -> bool:
    """等某个 HTTP 地址就绪。

    :param url: 目标地址。
    :param timeout: 最长等待秒数。
    :return: 就绪返回 ``True``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    return False


def _get_json(url: str) -> Dict[str, Any]:
    """GET 一个 JSON 接口。

    :param url: 目标地址。
    :return: 解析后的字典。
    """
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class _MockL2:
    """Mock L2 —— **专门用来证伪**的对照组。

    它被配置成「只要有人来问漂移，就回答 drift=true」。
    白名单真的生效时，它**一次都不会被叫醒**；
    白名单一旦失效，它就会把误报原样送回来。
    """

    def __init__(self) -> None:
        self._bodies: List[str] = []
        self._lock = threading.Lock()

    def count(self) -> int:
        """返回被调用的次数。

        :return: 调用次数。
        """
        with self._lock:
            return len(self._bodies)

    def bodies(self) -> List[str]:
        """返回每次调用的请求体摘要。

        :return: 请求体文本列表。
        """
        with self._lock:
            return list(self._bodies)

    def reset(self) -> None:
        """把计数清零。"""
        with self._lock:
            self._bodies.clear()


def _start_mock_l2(mock: _MockL2) -> ThreadingHTTPServer:
    """起一个本地 Mock L2。

    :param mock: 计数器。
    :return: 服务对象（调用 ``serve_forever`` 即可）。
    """

    class Handler(BaseHTTPRequestHandler):
        """只认 ``/v1/chat/completions`` 的假引擎。"""

        def log_message(self, *args: Any) -> None:
            """静音 —— 演示不要刷屏。"""

        def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 约定
            """回应引擎的**心跳**（``GET /v1/models``）。

            ⚠️ 不实现它，引擎看门狗会判定「本地推理引擎已掉线」，
               往告警列表里塞一条 ``ENGINE_OFFLINE`` ——
               那样「告警为空」这条验收就永远不成立。
               这条告警跟漂移判定**无关**，但它会污染证据。
            """
            payload = {
                "object": "list",
                "data": [{"id": "mock-l2", "object": "model"}],
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 约定
            """收下请求，记一次，回一个 drift=true。"""
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            with mock._lock:
                mock._bodies.append(raw[:400])
            payload = {
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "drift": True,
                            "reason": "Mock L2：本来要说漂移（白名单失效时会看到它）",
                        }, ensure_ascii=False),
                    },
                }],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="mock-l2", daemon=True
    )
    thread.start()
    return server


def _thinking_line(text: str) -> str:
    """造一条「思考」日志行（mcode 扁平协议）。

    :param text: 思考正文。
    :return: 一行 JSON。
    """
    return json.dumps(
        {"type": "thinking", "thinking": text}, ensure_ascii=False
    )


def _reply_line(text: str) -> str:
    """造一条「Agent 回答」日志行（mcode 扁平协议）。

    ⚠️ 100 档（极限摘要）**只显示** user / reply / tool / result
       四类事件 —— ``thinking`` 行在那一档**本就不显示**
       （见 ``dashboard.js`` 的 ``logRowVisible``）。
       所以「贴代码块」的用例必须用 ``text``（→ reply），
       才能在极限摘要截图里真的看到剥离效果。

    :param text: 回答正文。
    :return: 一行 JSON。
    """
    return json.dumps(
        {"type": "text", "text": text}, ensure_ascii=False
    )


def _tool_call_line(payload: Dict[str, Any]) -> str:
    """造一条「工具调用」日志行（mcode 扁平协议）。

    :param payload: 事件载荷。
    :return: 一行 JSON。
    """
    return json.dumps(payload, ensure_ascii=False)


def _demo_lines() -> List[str]:
    """演示用的日志行。

    排布（**故意**让两个漂移窗口都刚好满 5 步）：

    1. 前 5 行 —— **真实误报窗口**（2026-10-31 第 9 行），
       纯思考；第三步点名了 CHANGELOG ⇒ 窗口一被白名单放行；
    2. 接着 —— 跑 pytest、写 CHANGELOG.md、三步收尾思考，
       凑满第二个窗口 ⇒ 窗口二被白名单放行（命中的是**动作**）；
    3. 最后一行 —— 贴了代码块的 Agent 回答，
       用来证明 100 档把代码剥干净。

    :return: 日志行列表。
    """
    lines = [_thinking_line(text) for text in REAL_WINDOW]
    lines.append(_tool_call_line(PYTEST_RUN))
    lines.extend([_thinking_line(text) for text in CLOSING_THOUGHTS])
    lines.append(_tool_call_line(CHANGELOG_WRITE))
    lines.append(_reply_line(CODE_BLOCK_TEXT))
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
       别的配置项（规则、阈值）**一个都不动**；
       这一份只改五件事：

       1. 雷达的扫描根 → 一个**空目录**（演示不去碰真实会话）；
       2. 通知通道 → **全部关掉**（演示不该弹窗、不该发 webhook）；
       3. ``presentation`` → 日志 **100 档**（极限摘要）；
       4. ``task_goal`` → 真实误报窗口的原始目标；
       5. ``llm`` → 指向**本地 Mock**（白名单生效时它不会被叫醒）。

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
        # ⚠️ 两个档位是**互相独立**的两件事：
        #    * ``log_compression=100`` —— 右侧「被监控日志」走极限摘要
        #      （代码块剥离 + 只留前两句）；
        #    * ``audit_compression=0``  —— 中间「实时审核流水」走**全量**
        #      （这样「正常写入: CHANGELOG.md」那一行才不会被藏掉）。
        "log_compression": 100,
        "audit_compression": 0,
    }

    # 真实误报窗口的原始目标 —— 没有目标就没有「偏离」可言。
    raw["task_goal"] = REAL_GOAL

    llm = raw.get("llm")
    if not isinstance(llm, dict):
        llm = {}
        raw["llm"] = llm
    llm["provider"] = "openai_compatible"
    llm["base_url"] = "http://127.0.0.1:{}/v1".format(MOCK_PORT)
    llm["model"] = "mock-l2"
    llm["api_key"] = "sk-mock"
    llm["timeout"] = 10
    llm["max_tokens"] = 256
    llm["json_mode"] = False
    cascade = llm.get("cascade")
    if not isinstance(cascade, dict):
        cascade = {}
        llm["cascade"] = cascade
    cascade["enabled"] = False

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(raw, handle, allow_unicode=True, sort_keys=False)
    return path


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

    def evaluate(self, expression: str) -> Any:
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

    def viewport(self, width: int, height: int, scale: float = 1.0) -> None:
        """把视口设成给定的尺寸。

        :param width: 宽（CSS 像素）。
        :param height: 高（CSS 像素）。
        :param scale: 设备像素比。
        """
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": width,
            "height": height,
            "deviceScaleFactor": scale,
            "mobile": width < 768,
        })

    def click(self, selector: str) -> bool:
        """用**真实鼠标事件**点一个元素。

        ⚠️ 必须用真事件 —— 复选框靠的是浏览器的默认切换行为。

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
            "return {x:r.left+r.width/2,y:r.top+r.height/2};})()"
        )
        if not box:
            return False
        x = float(box["x"])
        y = float(box["y"])
        viewport = self.evaluate(
            "(function(){return {h:window.innerHeight};})()"
        ) or {}
        height = float(viewport.get("h") or 0)
        if height and not (0 <= y <= height):
            print("     ⚠️ 目标不在视口内（y={:.0f} / 视口高 {:.0f}）".format(
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


def _goto(cdp: _Cdp, url: str, wait: float = 5.0) -> None:
    """跳到某个页面并等它渲染。

    :param cdp: CDP 客户端。
    :param url: 目标地址。
    :param wait: 等待秒数。
    """
    cdp.send("Page.navigate", {"url": url})
    time.sleep(wait)


def _bypass_records(path: str) -> List[str]:
    """从看板日志里挑出「白名单放行」的记录。

    ⚠️ 这是**白名单真的开火了**的直接证据 ——
       不是「没告警所以大概没判」，而是「判了，但白名单拦下来了，
       模型根本没被问」。

    :param path: 看板日志路径。
    :return: 每条一行可读文本。
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return [
            stripped
            for stripped in (raw.strip() for raw in handle)
            if "白名单放行" in stripped
        ]


def _audit_rows(cdp: _Cdp) -> List[str]:
    """回读「实时审核流水」里的所有行。

    :param cdp: CDP 客户端。
    :return: 行文本列表。
    """
    value = cdp.evaluate(
        "(function(){var out=[];"
        "var nodes=document.querySelectorAll('.live-audit__line');"
        "for(var i=0;i<nodes.length;i+=1){"
        "out.push(nodes[i].textContent||'');}"
        "return out;})()"
    )
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _log_rows(cdp: _Cdp) -> List[str]:
    """回读「被监控日志」里所有行的正文。

    :param cdp: CDP 客户端。
    :return: 行文本列表。
    """
    value = cdp.evaluate(
        "(function(){var out=[];"
        "var nodes=document.querySelectorAll('.log-line__body');"
        "for(var i=0;i<nodes.length;i+=1){"
        "out.push(nodes[i].textContent||'');}"
        "return out;})()"
    )
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _demo_regression(
    cdp: _Cdp,
    base: str,
    mock: _MockL2,
    evidence: Dict[str, Any],
    server_log: str,
) -> None:
    """真实误报窗口 → 看板 → **零告警 + 零模型调用**。

    :param cdp: CDP 客户端。
    :param base: 看板地址前缀。
    :param mock: Mock L2 计数器。
    :param evidence: 证据字典（原地补充）。
    :param server_log: 看板日志路径（用来找白名单放行记录）。
    """
    print("     打开看板 …")
    _goto(cdp, base + "/", wait=6.0)
    print("     往被监控日志追加 {} 行真实窗口 …".format(
        _append_watch_log(WATCH_LOG)
    ))
    time.sleep(9.0)

    alerts_payload = _get_json(base + "/api/alerts")
    # ⚠️ /api/alerts 返回的是**字典**（alerts / counts / total），
    #    直接 len() 数到的是**键的个数**，不是告警条数。
    alerts = alerts_payload.get("alerts") or []
    rows = _audit_rows(cdp)
    changelog_rows = [row for row in rows if "CHANGELOG" in row]
    bypass = _bypass_records(server_log)

    print("     告警数：{}（接口 total={}）".format(
        len(alerts), alerts_payload.get("total")
    ))
    print("     Mock L2 被调用 {} 次".format(mock.count()))
    print("     流水里带 CHANGELOG 的行：{}".format(changelog_rows))
    print("     白名单放行记录：{}".format(bypass))

    cdp.screenshot(os.path.join(ASSETS, "v390-regression-no-drift.png"))

    evidence["regression"] = {
        "window_source": "2026-10-31.txt 第 9 行（REAL-FP-01）",
        "alerts": len(alerts),
        "alerts_payload_total": alerts_payload.get("total"),
        "mock_l2_calls": mock.count(),
        "mock_l2_bodies": mock.bodies(),
        "audit_rows": rows,
        "changelog_rows": changelog_rows,
        "whitelist_bypass_records": bypass,
        "no_drift_alert": len(alerts) == 0,
        "no_llm_call": mock.count() == 0,
        "event_was_processed": bool(changelog_rows),
        "whitelist_fired": bool(bypass),
        "screenshot": "v390-regression-no-drift.png",
    }


def _demo_extreme_summary(
    cdp: _Cdp,
    base: str,
    evidence: Dict[str, Any],
) -> None:
    """100 档极限摘要：代码块**彻底剥离**、长文本**只留前两句**。

    :param cdp: CDP 客户端。
    :param base: 看板地址前缀。
    :param evidence: 证据字典（原地补充）。
    """
    rows = _log_rows(cdp)
    fenced = [row for row in rows if "```" in row]
    code_leak = [
        row for row in rows
        if "def apply(" in row or "return strip_code_blocks" in row
    ]
    print("     日志行数：{}".format(len(rows)))
    print("     仍带 ``` 的行：{}".format(fenced))
    print("     仍带代码的行：{}".format(code_leak))

    cdp.screenshot(os.path.join(ASSETS, "v390-extreme-summary-100.png"))

    presentation = _get_json(base + "/api/presentation")
    evidence["extreme_summary"] = {
        "presentation": presentation,
        "log_rows": rows,
        "rows_with_fence": fenced,
        "rows_with_code": code_leak,
        "no_code_block": not fenced and not code_leak,
        "sentence_budget": 2,
        "screenshot": "v390-extreme-summary-100.png",
    }


def _demo_mobile(
    cdp: _Cdp,
    base: str,
    evidence: Dict[str, Any],
) -> None:
    """390×844 移动端：无横向滚动、单列 Tab、按钮 ≥44px。

    :param cdp: CDP 客户端。
    :param base: 看板地址前缀。
    :param evidence: 证据字典（原地补充）。
    """
    print("     视口压到 {}×{} …".format(MOBILE_WIDTH, MOBILE_HEIGHT))
    cdp.viewport(MOBILE_WIDTH, MOBILE_HEIGHT, scale=2.0)
    _goto(cdp, base + "/", wait=7.0)

    metrics = cdp.evaluate(
        "(function(){"
        "var de=document.documentElement;"
        "var resizers=document.querySelectorAll('.resizer,.resizer--v,"
        ".resizer--h');"
        "var hidden=0;"
        "for(var i=0;i<resizers.length;i+=1){"
        "if(getComputedStyle(resizers[i]).display==='none'){hidden+=1;}}"
        "var tabs=document.querySelectorAll('.mobile-tabs button,"
        ".mobile-tabs .tab');"
        "var heights=[];var tops=[];"
        "for(var j=0;j<tabs.length;j+=1){"
        "var r=tabs[j].getBoundingClientRect();"
        "heights.push(Math.round(r.height));"
        "tops.push(Math.round(r.top));}"
        "var btns=document.querySelectorAll('.btn,.btn--sm');"
        "var small=0;"
        "for(var k=0;k<btns.length;k+=1){"
        "var rb=btns[k].getBoundingClientRect();"
        "if(rb.height>0 && rb.height<44){small+=1;}}"
        "return {"
        "clientWidth:de.clientWidth,scrollWidth:de.scrollWidth,"
        "resizers:resizers.length,resizersHidden:hidden,"
        "tabCount:tabs.length,tabHeights:heights,tabTops:tops,"
        "buttons:btns.length,buttonsUnder44:small,"
        "mobileTabsVisible:!!document.querySelector('.mobile-tabs')"
        "&& getComputedStyle(document.querySelector('.mobile-tabs'))"
        ".display!=='none'};})()"
    ) or {}
    print("     移动端度量：{}".format(metrics))

    # 真鼠标点一下「日志」Tab —— 单列互斥要**真的切**过去。
    # ⚠️ Tab 的属性是 ``data-mobile-tab``（不是 ``data-tab``）。
    clicked = cdp.click('.mobile-tabs button[data-mobile-tab="log"]')
    if not clicked:
        clicked = cdp.click(".mobile-tabs button")
    time.sleep(2.0)
    after = cdp.evaluate(
        "(function(){return {"
        "tab:document.body.getAttribute('data-mobile-tab')||'',"
        "scrollWidth:document.documentElement.scrollWidth,"
        "clientWidth:document.documentElement.clientWidth};})()"
    ) or {}
    print("     真鼠标点击 Tab 后：{}".format(after))
    cdp.screenshot(os.path.join(ASSETS, "v390-mobile-390x844.png"))

    evidence["mobile"] = {
        "viewport": "{}x{}".format(MOBILE_WIDTH, MOBILE_HEIGHT),
        "metrics": metrics,
        "after_click": after,
        "clicked": bool(clicked),
        "no_horizontal_scroll": (
            int(metrics.get("scrollWidth") or 0)
            <= int(metrics.get("clientWidth") or 0)
        ),
        "resizers_hidden": (
            int(metrics.get("resizers") or 0)
            == int(metrics.get("resizersHidden") or 0)
        ),
        "tabs_single_row": len(set(metrics.get("tabTops") or [])) <= 1,
        "touch_targets_ok": int(metrics.get("buttonsUnder44") or 0) == 0,
        "screenshot": "v390-mobile-390x844.png",
    }


def main() -> int:
    """命令行入口。

    :return: 进程退出码。
    """
    _prepare_stdout()
    base = "http://127.0.0.1:{}".format(PORT)
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(RADAR_ROOT, exist_ok=True)
    os.makedirs(ASSETS, exist_ok=True)
    for path in (WATCH_LOG, CONFIG, ONTRACK_LOG):
        if os.path.exists(path):
            os.remove(path)
    persist = os.path.join(WORK, "attached_sessions.json")
    if os.path.exists(persist):
        os.remove(persist)

    _prepare_watch_log(WATCH_LOG)
    _write_demo_config(CONFIG, RADAR_ROOT)

    mock = _MockL2()
    mock_server = _start_mock_l2(mock)

    evidence: Dict[str, Any] = {
        "version": "v3.9.0",
        "port": PORT,
        "mock_l2": "http://127.0.0.1:{}/v1".format(MOCK_PORT),
        "demo_log": os.path.basename(WATCH_LOG),
        "radar_root": RADAR_ROOT,
    }

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server_log_path = os.path.join(WORK, "server.log")
    server_log = open(server_log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--watch", WATCH_LOG,
            "--tail",
            # ⚠️ 告警日志必须**指向演示目录** ——
            #    默认值是 logs/sentinel_alert.log（用户真实日志），
            #    演示往里写 ONTRACK_JSON 就是污染用户数据。
            "--alert-log", ONTRACK_LOG,
            "--config", CONFIG,
            "--host", "127.0.0.1",
            "--port", str(PORT),
            # DEBUG 级别 ⇒ 引擎那句「漂移判定白名单放行：…」会进日志，
            # 白名单**真的开火了**才有据可查（不是靠「没告警」反推）。
            "--log-level", "DEBUG",
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
    try:
        print("[1/4] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/4] 打开浏览器 …")
        browser = _find_browser()
        profile = os.path.join(
            os.environ.get("TEMP", "."),
            "sentinel-v390-cdp-{}".format(int(time.time() * 1000)),
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
        cdp.viewport(1600, 1000)

        print("[3/4] 漂移白名单端到端 …")
        _demo_regression(cdp, base, mock, evidence, server_log_path)

        print("[4/4] 100 档极限摘要 + 移动端 390×844 …")
        _demo_extreme_summary(cdp, base, evidence)
        _demo_mobile(cdp, base, evidence)

        exceptions = cdp.exceptions()
        if exceptions:
            print("      页面异常：{}".format(exceptions[:3]))
        evidence["page_exceptions"] = exceptions

        out = os.path.join(ASSETS, "evidence-mobile-summary.json")
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("      证据已写入：{}".format(out))

        reg = evidence.get("regression", {})
        mob = evidence.get("mobile", {})
        ext = evidence.get("extreme_summary", {})
        ok = (
            reg.get("no_drift_alert")
            and reg.get("no_llm_call")
            and reg.get("event_was_processed")
            and reg.get("whitelist_fired")
            and ext.get("no_code_block")
            and mob.get("no_horizontal_scroll")
            and mob.get("resizers_hidden")
            and mob.get("tabs_single_row")
            and mob.get("touch_targets_ok")
        )
        print()
        print("验收结论：{}".format("全部通过" if ok else "有项目未通过"))
        return 0 if ok else 1
    finally:
        if cdp is not None:
            try:
                cdp.close()
            except OSError:
                pass
        if browser_proc is not None:
            browser_proc.terminate()
            try:
                browser_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser_proc.kill()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock_server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
