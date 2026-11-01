"""v3.6.1 验收演示 —— 修复中间上下拖拽条的方向反向。

它做的事**每一步都是真的**
==========================

起**真的**看板进程（``src.gui.server``）+ **真的**浏览器，
用 CDP 的**真实鼠标事件**（mousePressed / mouseMoved / mouseReleased）
去拖四条分隔条，然后量**真实几何**：

* 往下拖 ``#resizer-audit`` → **把手自己**跟着鼠标下移，
  实时审核流水区**向下长大**，实时告警区**缩小**；
* 往上拖 ``#resizer-audit`` → **把手自己**跟着鼠标上移，
  流水区收缩，告警区**长大**；
* 往右拖 ``#resizer-sessions`` → 会话栏变宽，把手跟着鼠标；
* 往左拖 ``#resizer-stream`` → 日志面板变宽，把手跟着鼠标。

⚠️ **v3.6.0 的验收漏洞就在这里** ——
   上一版只断言了「流水区盒子变高」，
   **没有量把手自己的位移**，
   所以 30/30 全过却漏掉了「把手反向跑」这个用户可见的 Bug。
   这一版**两个都量、两个都断言**。

⚠️ 演示用的配置文件写在 ``logs/v361-demo/`` 下，
   **不会**写回 ``config/config.yaml``；
   它只改了一件事：雷达的扫描根。
   挂载持久化（``attached_sessions.json``）也落在同一个目录里，
   **绝不**碰用户真实的 ``config/attached_sessions.json``。
⚠️ 演示目录名**不能**命中雷达的默认排除词
   （``sentinel`` / ``stress`` / ``test``）。

用法::

    python _构建与发布/scripts/demo_v361_drag_direction.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import yaml

#: 仓库根目录。
ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

#: 看板端口（跟 v3.6.0 演示的 8153 错开，免得撞车）。
PORT = 8154

#: CDP 调试端口。
CDP_PORT = 9364

#: 演示工作目录。
WORK = os.path.join(ROOT, "logs", "v361-demo")

#: 雷达扫描根 —— 指向一个**空目录**，演示不去碰任何真实会话。
RADAR_ROOT = os.path.join(WORK, "radar-root")

#: 被监控的演示日志（mcode 扁平协议）。
WATCH_LOG = os.path.join(WORK, "mcode.jsonl")

#: 演示专用配置（**只**改雷达扫描根）。
CONFIG = os.path.join(WORK, "v361-config.yaml")

#: 截图与证据的落点。
ASSETS = os.path.join(ROOT, "_管理文档", "文档与报告", "assets")

#: 候选浏览器。
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

#: 纵向拖拽的步长（CSS 像素）。
VERTICAL_STEP = 90

#: 横向拖拽的步长（CSS 像素）。
HORIZONTAL_STEP = 120

#: 几何断言的容差（像素）。
#:
#: ⚠️ 浏览器会把小数坐标量化到整数像素，
#:    再加上 Flex 布局的舍入，差个 1-2 像素是正常的。
TOLERANCE = 4


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


# ===========================================================================
# 演示素材
# ===========================================================================
def _demo_lines() -> List[Dict[str, Any]]:
    """演示用的流水事件行。

    :return: 事件字典列表。
    """
    return [
        {"type": "user_message", "content": "请帮我把 auth.py 的登录逻辑重构一遍。"},
        {"type": "thinking", "text": "先确认这个模块的边界条件。"},
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/auth.py"},
            },
        },
        {"type": "text", "text": "已经改好了：login() 拆成两个纯函数。"},
    ]


def _prepare_watch_log(path: str) -> str:
    """先把被监控日志**建成空文件**。

    ⚠️ 看板的常驻监听是**从文件末尾开始**跟的 ——
       启动前写好的内容它看不到，也就不会过规则、不出流水。
       所以顺序必须是：**先建空文件 → 起服务 → 再追加**。

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
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(body + "\n")
        handle.flush()
    return len(lines)


def _write_demo_config(path: str, scan_root: str) -> str:
    """写一份演示专用配置：只改**雷达的扫描根**。

    ⚠️ 用的是 ``config/config.example.yaml`` 当底稿，
       别的配置项（规则、阈值、通知通道、模型层）**一个都不动**；
       这份配置只存在于 ``logs/v361-demo/`` 下，
       **不会**写回 ``config/config.yaml``。

    :param path: 输出路径。
    :param scan_root: 雷达要扫的目录（演示里指向一个**空目录**）。
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

    def mouse(self, kind: str, x: float, y: float, buttons: int) -> None:
        """发一个**真实**鼠标事件。

        ⚠️ 必须用真事件 —— 分隔条上挂的是 ``pointerdown``，
           合成事件不会触发它。

        :param kind: ``mousePressed`` / ``mouseMoved`` / ``mouseReleased``。
        :param x: 视口横坐标（CSS 像素）。
        :param y: 视口纵坐标（CSS 像素）。
        :param buttons: 当前按下的键位掩码。
        """
        self.send("Input.dispatchMouseEvent", {
            "type": kind,
            "x": float(x),
            "y": float(y),
            "button": "left",
            "buttons": int(buttons),
            "clickCount": 1 if kind == "mousePressed" else 0,
        })

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


def _center_of(cdp: _Cdp, selector: str) -> Optional[Tuple[float, float]]:
    """取一个元素在视口里的中心点。

    :param cdp: CDP 客户端。
    :param selector: CSS 选择器。
    :return: ``(x, y)``；元素不在就是 ``None``。
    """
    value = cdp.evaluate(
        "(function(){var n=document.querySelector("
        + json.dumps(selector) + ");"
        "if(!n){return null;}"
        "var r=n.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2};})()"
    )
    if not isinstance(value, dict):
        return None
    return float(value.get("x") or 0), float(value.get("y") or 0)


def _drag(
    cdp: _Cdp,
    selector: str,
    dx: float,
    dy: float,
    steps: int = 10,
) -> bool:
    """用**真实鼠标事件**拖一个元素。

    :param cdp: CDP 客户端。
    :param selector: CSS 选择器。
    :param dx: 横向位移（CSS 像素）。
    :param dy: 纵向位移（CSS 像素）。
    :param steps: 分几步移动（步数多更像真人）。
    :return: 起点找没找到。
    """
    center = _center_of(cdp, selector)
    if center is None:
        return False
    x0, y0 = center
    cdp.mouse("mousePressed", x0, y0, 1)
    for index in range(1, steps + 1):
        ratio = index / float(steps)
        cdp.mouse(
            "mouseMoved",
            x0 + dx * ratio,
            y0 + dy * ratio,
            1,
        )
        time.sleep(0.03)
    cdp.mouse("mouseReleased", x0 + dx, y0 + dy, 0)
    time.sleep(0.4)
    return True


# ===========================================================================
# 量几何的 JS
# ===========================================================================
_JS_GEOMETRY = """
(function(){
  function box(sel){
    var n=document.querySelector(sel);
    if(!n){return null;}
    var r=n.getBoundingClientRect();
    return {top:Math.round(r.top),bottom:Math.round(r.bottom),
            left:Math.round(r.left),right:Math.round(r.right),
            width:Math.round(r.width),height:Math.round(r.height)};
  }
  var p=document.getElementById('alert-panel');
  var cs=p?window.getComputedStyle(p):null;
  var order=[];
  if(p){
    for(var i=0;i<p.children.length;i++){
      var c=p.children[i];
      order.push(c.id||c.className||c.tagName);
    }
  }
  return {
    resizer_audit: box('#resizer-audit'),
    flow: box('.live-audit'),
    alert_head: box('#alert-panel .panel__head'),
    alert_body: box('#alert-panel .panel__body'),
    panel: box('#alert-panel'),
    resizer_sessions: box('#resizer-sessions'),
    sessions: box('#session-panel'),
    resizer_stream: box('#resizer-stream'),
    stream: box('#stream-panel'),
    audit_h_var: cs?cs.getPropertyValue('--audit-h').trim():null,
    panel_children: order
  };
})()
"""


def _num(value: Any) -> int:
    """把可能为 ``None`` 的几何值转成整数。

    :param value: 原值。
    :return: 整数（拿不到就是 0）。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _delta(before: Any, after: Any, key: str) -> int:
    """算两个几何对象在某个方向上的差。

    :param before: 拖之前的几何。
    :param after: 拖之后的几何。
    :param key: ``top`` / ``left`` / ``width`` / ``height``。
    :return: 差值（像素）。
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return 0
    return _num(after.get(key)) - _num(before.get(key))


def _report(tag: str, before: Dict[str, Any], after: Dict[str, Any]) -> None:
    """把拖前拖后的几何差打印出来。

    :param tag: 场景名。
    :param before: 拖之前的几何。
    :param after: 拖之后的几何。
    """
    print("      [{}]".format(tag))
    print("        把手(纵) top   : {:+5d}".format(
        _delta(before.get("resizer_audit"),
               after.get("resizer_audit"), "top")
    ))
    print("        流水区   height: {:+5d}".format(
        _delta(before.get("flow"), after.get("flow"), "height")
    ))
    print("        告警区   height: {:+5d}".format(
        _delta(before.get("alert_body"), after.get("alert_body"), "height")
    ))
    print("        面板     height: {:+5d}".format(
        _delta(before.get("panel"), after.get("panel"), "height")
    ))


# ===========================================================================
# 演示主体
# ===========================================================================
def _demo_vertical(cdp: _Cdp, evidence: Dict[str, Any]) -> None:
    """演示一：上下拖 —— 把手跟手，流水区与告警区此消彼长。

    :param cdp: CDP 客户端。
    :param evidence: 证据字典（原样修改）。
    """
    print("[3/7] 量拖拽前的几何 …")
    before = cdp.evaluate(_JS_GEOMETRY) or {}
    print("      把手 top={} / 流水高={} / 告警区高={} / 面板高={}".format(
        _num((before.get("resizer_audit") or {}).get("top")),
        _num((before.get("flow") or {}).get("height")),
        _num((before.get("alert_body") or {}).get("height")),
        _num((before.get("panel") or {}).get("height")),
    ))
    print("      中间面板的子元素顺序：{}".format(
        before.get("panel_children")
    ))

    print("[4/7] 往下拖中间分隔条 {}px …".format(VERTICAL_STEP))
    _drag(cdp, "#resizer-audit", 0, VERTICAL_STEP)
    down = cdp.evaluate(_JS_GEOMETRY) or {}
    _report("往下拖", before, down)
    shot_down = os.path.join(ASSETS, "v361-drag-down.png")
    cdp.screenshot(shot_down)

    print("[5/7] 往上拖中间分隔条 {}px …".format(VERTICAL_STEP))
    _drag(cdp, "#resizer-audit", 0, -VERTICAL_STEP)
    up = cdp.evaluate(_JS_GEOMETRY) or {}
    _report("往上拖", down, up)
    shot_up = os.path.join(ASSETS, "v361-drag-up.png")
    cdp.screenshot(shot_up)

    handle_down = _delta(
        before.get("resizer_audit"), down.get("resizer_audit"), "top"
    )
    flow_down = _delta(before.get("flow"), down.get("flow"), "height")
    body_down = _delta(
        before.get("alert_body"), down.get("alert_body"), "height"
    )
    handle_up = _delta(
        down.get("resizer_audit"), up.get("resizer_audit"), "top"
    )
    flow_up = _delta(down.get("flow"), up.get("flow"), "height")
    body_up = _delta(
        down.get("alert_body"), up.get("alert_body"), "height"
    )
    panel_delta = _delta(before.get("panel"), down.get("panel"), "height")

    children = before.get("panel_children") or []
    flow_index = -1
    handle_index = -1
    for index, name in enumerate(children):
        if "live-audit" in str(name):
            flow_index = index
        if "resizer-audit" in str(name):
            handle_index = index

    evidence["vertical"] = {
        "before": before,
        "after_down": down,
        "after_up": up,
        "handle_follows_mouse_down": abs(handle_down - VERTICAL_STEP) <= TOLERANCE,
        "handle_follows_mouse_up": abs(handle_up + VERTICAL_STEP) <= TOLERANCE,
        "flow_grows_on_drag_down": abs(flow_down - VERTICAL_STEP) <= TOLERANCE,
        "flow_shrinks_on_drag_up": abs(flow_up + VERTICAL_STEP) <= TOLERANCE,
        "alert_shrinks_on_drag_down": abs(body_down + VERTICAL_STEP) <= TOLERANCE,
        "alert_grows_on_drag_up": abs(body_up - VERTICAL_STEP) <= TOLERANCE,
        "panel_height_unchanged": abs(panel_delta) <= 2,
        "flow_above_handle_in_dom": (
            0 <= flow_index < handle_index
        ),
        "handle_delta_down": handle_down,
        "handle_delta_up": handle_up,
        "flow_delta_down": flow_down,
        "flow_delta_up": flow_up,
        "alert_delta_down": body_down,
        "alert_delta_up": body_up,
        "screenshot_down": os.path.basename(shot_down),
        "screenshot_up": os.path.basename(shot_up),
    }


def _demo_horizontal(cdp: _Cdp, evidence: Dict[str, Any]) -> None:
    """演示二：左右拖 —— 行为**一字未改**。

    :param cdp: CDP 客户端。
    :param evidence: 证据字典（原样修改）。
    """
    print("[6/7] 往右拖会话栏分隔条 {}px …".format(HORIZONTAL_STEP))
    before = cdp.evaluate(_JS_GEOMETRY) or {}
    _drag(cdp, "#resizer-sessions", HORIZONTAL_STEP, 0)
    right = cdp.evaluate(_JS_GEOMETRY) or {}

    print("      往左拖日志分隔条 {}px …".format(HORIZONTAL_STEP))
    _drag(cdp, "#resizer-stream", -HORIZONTAL_STEP, 0)
    left = cdp.evaluate(_JS_GEOMETRY) or {}
    shot = os.path.join(ASSETS, "v361-drag-horizontal.png")
    cdp.screenshot(shot)

    sessions_handle = _delta(
        before.get("resizer_sessions"),
        right.get("resizer_sessions"),
        "left",
    )
    sessions_width = _delta(
        before.get("sessions"), right.get("sessions"), "width"
    )
    stream_handle = _delta(
        right.get("resizer_stream"),
        left.get("resizer_stream"),
        "left",
    )
    stream_width = _delta(
        right.get("stream"), left.get("stream"), "width"
    )

    print("      会话栏把手 left {:+d} / 会话栏宽 {:+d}".format(
        sessions_handle, sessions_width
    ))
    print("      日志把手   left {:+d} / 日志宽   {:+d}".format(
        stream_handle, stream_width
    ))

    evidence["horizontal"] = {
        "before": before,
        "after_sessions": right,
        "after_stream": left,
        "sessions_handle_follows": (
            abs(sessions_handle - HORIZONTAL_STEP) <= TOLERANCE
        ),
        "sessions_widens": abs(sessions_width - HORIZONTAL_STEP) <= TOLERANCE,
        "stream_handle_follows": (
            abs(stream_handle + HORIZONTAL_STEP) <= TOLERANCE
        ),
        "stream_widens": abs(stream_width - HORIZONTAL_STEP) <= TOLERANCE,
        "sessions_handle_delta": sessions_handle,
        "sessions_width_delta": sessions_width,
        "stream_handle_delta": stream_handle,
        "stream_width_delta": stream_width,
        "screenshot": os.path.basename(shot),
    }


def _demo_persistence(cdp: _Cdp, evidence: Dict[str, Any]) -> None:
    """演示三：刷新之后比例还在（顺手确认没把持久化弄坏）。

    :param cdp: CDP 客户端。
    :param evidence: 证据字典（原样修改）。
    """
    print("[7/7] 刷新页面，看比例还在不在 …")
    before = cdp.evaluate(_JS_GEOMETRY) or {}
    cdp.send("Page.reload", {"ignoreCache": True})
    time.sleep(6.0)
    after = cdp.evaluate(_JS_GEOMETRY) or {}
    flow_before = _num((before.get("flow") or {}).get("height"))
    flow_after = _num((after.get("flow") or {}).get("height"))
    print("      刷新前流水高 {} / 刷新后流水高 {}".format(
        flow_before, flow_after
    ))
    evidence["persistence"] = {
        "flow_height_before": flow_before,
        "flow_height_after": flow_after,
        "flow_height_kept": abs(flow_after - flow_before) <= 4,
    }


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
    # ⚠️ 挂载持久化 ——
    #    上一轮演示挂过的会话会被**自动接回来**。删掉它，
    #    这份持久化也就落在演示目录里，**绝不**碰用户真实的那份。
    persist = os.path.join(WORK, "attached_sessions.json")
    if os.path.exists(persist):
        os.remove(persist)

    _prepare_watch_log(WATCH_LOG)
    config = _write_demo_config(CONFIG, RADAR_ROOT)

    evidence: Dict[str, Any] = {
        "version": "v3.6.1",
        "port": PORT,
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
        print("[1/7] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/7] 打开浏览器 …")
        browser = _find_browser()
        profile = os.path.join(
            os.environ.get("TEMP", "."),
            "sentinel-v361-cdp-{}".format(int(time.time() * 1000)),
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
        cdp.send("Page.navigate", {"url": base + "/"})
        time.sleep(6.0)

        print("     往被监控日志追加 {} 行演示事件 …".format(
            _append_watch_log(WATCH_LOG)
        ))
        time.sleep(3.0)

        _demo_vertical(cdp, evidence)
        _demo_horizontal(cdp, evidence)
        _demo_persistence(cdp, evidence)

        print("     写证据 …")
        report = os.path.join(ASSETS, "v361-evidence.json")
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("     证据 → {}".format(report))

        vertical = evidence.get("vertical", {})
        horizontal = evidence.get("horizontal", {})
        persistence = evidence.get("persistence", {})

        checks = [
            ("流水区在分隔条上方", vertical.get("flow_above_handle_in_dom")),
            ("往下拖·把手跟手下移", vertical.get("handle_follows_mouse_down")),
            ("往上拖·把手跟上移", vertical.get("handle_follows_mouse_up")),
            ("往下拖·流水区变高", vertical.get("flow_grows_on_drag_down")),
            ("往上拖·流水区变矮", vertical.get("flow_shrinks_on_drag_up")),
            ("往下拖·告警区变矮", vertical.get("alert_shrinks_on_drag_down")),
            ("往上拖·告警区变高", vertical.get("alert_grows_on_drag_up")),
            ("拖拽不改面板总高", vertical.get("panel_height_unchanged")),
            ("往右拖·会话把手跟手", horizontal.get("sessions_handle_follows")),
            ("往右拖·会话栏变宽", horizontal.get("sessions_widens")),
            ("往左拖·日志把手跟手", horizontal.get("stream_handle_follows")),
            ("往左拖·日志面板变宽", horizontal.get("stream_widens")),
            ("刷新后比例还在", persistence.get("flow_height_kept")),
        ]
        failed = [name for name, ok in checks if not ok]
        print("")
        print("验收结论：{}".format(
            "全部通过 ✓" if not failed else "有项目没通过 ✗"
        ))
        for name, ok in checks:
            print("  {:22s} → {}".format(name, "✓" if ok else "✗"))
        if failed:
            print("\n没通过：{}".format("、".join(failed)))
        return 0 if not failed else 1
    finally:
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
        # 浏览器 profile 是**自己**建的，就得**自己**收走。
        if profile and os.path.isdir(profile):
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
