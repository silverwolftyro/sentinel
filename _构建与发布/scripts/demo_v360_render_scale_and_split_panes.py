"""v3.6.0 验收演示 —— 无极调节渲染 · 多面板拖拽 · 智能上下文提取。

它做的事**每一步都是真的**
==========================

1. **无极调节渲染（任务二）** ——
   起**真的**看板（``src.gui.server``），喂它一份**真的** MCode 日志
   （含 ``user_message`` / ``text`` / ``thinking`` / ``tool_call`` /
   ``tool_result`` / 系统噪声），然后**真的**把滑条拨到 0 / 50 / 100，
   分别量 DOM 上的文字：

   * 0 档 —— 原始 JSON 还在（含 ``arguments``、噪声标签）；
   * 50 档 —— 🧑 用户提问 / 🤖 Agent 回答 / 💭 思考 都出现了，
     ``arguments`` **没了**，工具结果**藏起来了**；
   * 100 档 —— 💭 思考**不见了**，问题 + 回答 + 工具调用还在。

   还额外证明**切档位不丢内容**：0 → 50 → 100 → 0 走一圈，
   回到 0 档时文字与一开始**逐字相同**（说明重绘走的是缓存，不是重拉）。

2. **多面板拖拽与折叠（任务三）** ——
   用 CDP 的**真实鼠标事件**（mousePressed / mouseMoved / mouseReleased）
   去拖三条分隔条，量**真实几何**：

   * 往右拖 ``#resizer-sessions`` → 会话栏变宽；
   * 往左拖 ``#resizer-stream`` → 日志面板变宽；
   * 往下拖 ``#resizer-audit`` → 流水区变高。

   然后点折叠按钮：会话栏收成**极窄侧边条**（46px），
   窄条上显示**会话数量**；再点展开回原状。
   最后**刷新页面**，量宽度 —— 比例**还在**（localStorage 持久化）。

3. **路径校验与空态（任务一）** ——
   雷达指向一个**空目录**（所以真的扫不到任何 Agent），
   空态给的是**相对路径** ``logs/capture.jsonl`` +
   「请先运行该命令，生成文件后再点击接入。」；
   再往手动输入框里填一个**根本不存在**的路径，点【接入】——
   前端**先问后端**文件在不在，不在就**当场飘红字**，
   **一个会话都没被接进来**。

⚠️ 演示用的配置文件写在 ``logs/v360-demo/`` 下，
   **不会**写回 ``config/config.yaml``；
   它只改了一件事：雷达的扫描根。
⚠️ 演示目录名**不能**命中雷达的默认排除词
   （``sentinel`` / ``stress`` / ``test``），否则会被当成历史会话收起来。

用法::

    python _构建与发布/scripts/demo_v360_render_scale_and_split_panes.py
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

#: 看板端口（跟 v3.5.1 的 8151、v3.5.2 的 8152 错开，免得撞车）。
PORT = 8153

#: CDP 调试端口。
CDP_PORT = 9363

#: 演示工作目录。
WORK = os.path.join(ROOT, "logs", "v360-demo")

#: 雷达扫描根 —— **故意是空的**，好让雷达真的扫不到东西，
#: 从而证明空态给的是「相对路径 + 先生成再接入」的提示。
RADAR_ROOT = os.path.join(WORK, "radar-root")

#: 被监控的演示日志（mcode 扁平协议）。
WATCH_LOG = os.path.join(WORK, "mcode.jsonl")

#: 截图与证据的落点。
ASSETS = os.path.join(ROOT, "_管理文档", "文档与报告", "assets")

#: 演示里**故意**填的不存在路径。
MISSING_PATH = os.path.join(WORK, "never-created.jsonl")

#: 用户在演示里真正问的那句话。
#:
#: ⚠️ 刻意**压到 40 字以内** —— 流水区引用的原文最多留 40 字
#:    （前端 AUDIT_SNIPPET_LIMIT 与后端 MAX_SNIPPET_LENGTH 同口径），
#:    超了就只能看到前半句。这样演示才能断言「气泡里是**逐字**的真实原话」。
USER_GOAL = "请帮我把 auth.py 的登录逻辑重构一遍，并补上单元测试。"

#: Agent 真正回答的那句话。
AGENT_REPLY = "已经改好了：login() 拆成两个纯函数，12 个单测全过。"

#: 思考里的一句话。
THINK_TEXT = "先确认这个模块的边界条件，再决定改不改签名。"

#: 工具结果里的一句话（自然语言档应当把它藏起来）。
TOOL_RESULT_TEXT = "读到了 218 行，含 login() 与 refresh() 两个入口。"

#: 噪声段落后面真正要紧的话 —— 证明「挖段落」而不是「整行丢」。
NOISE_TAIL = "刚才那个问题还没解决，请继续处理。"

#: 空态时给出的相对路径（必须与前端常量一致）。
RADAR_RELATIVE_PATH = "logs/capture.jsonl"

#: 空态时给出的提示语（必须与前端常量一致）。
RADAR_MISSING_NOTE = "请先运行该命令，生成文件后再点击接入。"

#: 手动接入被拦下时的红字（必须与前端常量一致）。
ATTACH_ERROR_TEXT = "文件不存在，请检查路径或重新生成"


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


# ===========================================================================
# 演示素材
# ===========================================================================
def _demo_lines() -> List[Dict[str, Any]]:
    """演示用的 MCode 扁平协议事件行。

    ⚠️ 内容是**故意**这么排的 —— 每一行都在证明一件事：

    * ``user_message`` → 自然语言档的「🧑 用户提问」；
    * ``thinking`` → 自然语言档的「💭 思考」；
    * ``tool_call`` → 工具调用（**带 arguments**，用来证明自然语言档
      会把参数藏掉）；
    * ``tool_result`` → 工具结果（用来证明自然语言档会把它藏起来）；
    * ``text`` → 「🤖 Agent 回答」；
    * 带 ``<environment_context>`` 的那条 → 用来证明噪声是
      **按段落挖掉**的，**问题本身没丢**；
    * 认不出来的那条 → 「· 其他」。

    :return: 事件字典列表。
    """
    return [
        {"type": "user_message", "content": USER_GOAL},
        {"type": "thinking", "text": THINK_TEXT},
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {"file_path": "src/core/auth.py"},
            },
        },
        {"type": "tool_result", "content": TOOL_RESULT_TEXT},
        {
            "type": "user_message",
            "content": (
                "<environment_context>\n"
                "<cwd>D:\\项目\\<项目目录></cwd>\n"
                "</environment_context>\n" + NOISE_TAIL
            ),
        },
        {"type": "text", "text": AGENT_REPLY},
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Bash",
                "arguments": {"command": "python -m pytest -q tests/"},
            },
        },
        {"type": "some_unknown_event", "payload": {"x": 1}},
    ]


def _prepare_watch_log(path: str) -> str:
    """先把被监控日志**建成空文件**。

    ⚠️ 为什么先建空文件，而不是直接把内容写进去？

    看板常驻监听（``--tail``）是**从文件末尾开始**跟的
    （``from_end=True``）—— 启动前就写好的内容，引擎**看不到**，
    也就不会过规则、不产生「实时审核流水」。

    所以顺序必须是：**先建空文件 → 起服务 → 再往里追加**，
    这样追加的每一行都是引擎眼里的「新事件」。

    :param path: 目标路径。
    :return: 路径字符串。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8"):
        pass
    return path


def _append_watch_log(path: str) -> int:
    """往被监控日志里**真的追加**演示事件（模拟 Agent 正在写）。

    :param path: 日志路径。
    :return: 追加的行数。
    """
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in _demo_lines()
    )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(body + "\n")
        handle.flush()
    return len(_demo_lines())


def _command_target_path(command: str) -> str:
    """从空态给出的那条命令里，**取出重定向目标路径**。

    ⚠️ ``.radar-empty__command`` 的 textContent 是**整条命令**
    （形如 ``mcode exec "你的指令" > logs/capture.jsonl``），
    所以不能拿它整段跟路径比 —— 得先把目标抠出来。

    :param command: 页面上的完整命令文本。
    :return: 重定向目标路径；抠不出来时返回空串。
    """
    if ">" not in command:
        return ""
    return command.rsplit(">", 1)[-1].strip()


def _write_demo_config(path: str, scan_root: str) -> str:
    """写一份演示专用配置：只改**雷达的扫描根**。

    ⚠️ 用的是 ``config/config.example.yaml`` 当底稿，
    别的配置项（规则、阈值、通知通道、模型层）**一个都不动**；
    这份配置只存在于 ``logs/v360-demo/`` 下，
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
    steps: int = 8,
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
        time.sleep(0.02)
    cdp.mouse("mouseReleased", x0 + dx, y0 + dy, 0)
    time.sleep(0.4)
    return True


def _set_scale(cdp: _Cdp, value: int) -> str:
    """把日志渲染档位滑条拨到某一档。

    ⚠️ 走的是**真实的** ``input`` 事件 —— 真实用户拖滑条就是这条路。

    :param cdp: CDP 客户端。
    :param value: 目标档位（0-100）。
    :return: 拨完之后的档位标签。
    """
    cdp.evaluate(
        "(function(){var s=document.getElementById('log-scale');"
        "if(!s){return null;}"
        "s.value=" + json.dumps(str(value)) + ";"
        "s.dispatchEvent(new Event('input',{bubbles:true}));"
        "return true;})()"
    )
    time.sleep(0.8)
    return str(
        cdp.evaluate(
            "(function(){var n=document.getElementById('log-scale-value');"
            "return n?n.textContent:'';})()"
        )
        or ""
    )


def _log_text(cdp: _Cdp) -> str:
    """取日志面板当前显示的全部文字。

    :param cdp: CDP 客户端。
    :return: 拼接起来的文字。
    """
    return str(
        cdp.evaluate(
            "(function(){var n=document.getElementById('log-lines');"
            "return n?n.textContent:'';})()"
        )
        or ""
    )


# ===========================================================================
# 量几何的 JS
# ===========================================================================
_JS_GEOMETRY = """
(function(){
  var body=document.getElementById('body-layout');
  var sessions=document.getElementById('session-panel');
  var stream=document.getElementById('stream-panel');
  var audit=document.querySelector('.live-audit');
  var rail=document.getElementById('sessions-rail');
  var cs=function(n){return n?getComputedStyle(n):null;};
  function rect(n){
    if(!n){return null;}
    var r=n.getBoundingClientRect();
    return {left:Math.round(r.left),top:Math.round(r.top),
            width:Math.round(r.width),height:Math.round(r.height),
            right:Math.round(r.right),bottom:Math.round(r.bottom)};
  }
  return {
    body_class: body?body.className:'',
    sessions_w_var: body?cs(body).getPropertyValue('--sessions-w').trim():null,
    stream_w_var: body?cs(body).getPropertyValue('--stream-w').trim():null,
    audit_h_var: (function(){var p=document.getElementById('alert-panel');
      return p?cs(p).getPropertyValue('--audit-h').trim():null;})(),
    alert_panel_rect: rect(document.getElementById('alert-panel')),
    sessions_rect: rect(sessions),
    stream_rect: rect(stream),
    audit_rect: rect(audit),
    rail_hidden: rail?rail.hidden:null,
    rail_count: (function(){
      var n=document.getElementById('sessions-rail-count');
      return n?n.textContent:null;})(),
    resizers_visible: (function(){
      var ids=['resizer-sessions','resizer-audit','resizer-stream'];
      var out={};
      for(var i=0;i<ids.length;i++){
        var n=document.getElementById(ids[i]);
        out[ids[i]]=n?cs(n).display:null;
      }
      return out;})()
  };
})()
"""


# ===========================================================================
# 演示一：无极调节渲染
# ===========================================================================
def _demo_render_scale(
    cdp: _Cdp,
    evidence: Dict[str, Any],
) -> None:
    """演示一：档位 0 / 50 / 100 的三种渲染效果。

    :param cdp: CDP 客户端。
    :param evidence: 证据字典（原样修改）。
    """
    print("[3/9] 等日志面板出现内容 …")
    got = _wait_for(
        cdp,
        "document.querySelectorAll('#log-lines .log-line').length >= 5",
        timeout=40,
    )
    print("      日志行数：{}".format(got))
    time.sleep(1.0)

    shots: Dict[str, Any] = {}
    texts: Dict[str, str] = {}

    for value, label, name in (
        (0, "纯代码", "v360-log-scale-0.png"),
        (50, "自然语言", "v360-log-scale-50.png"),
        (100, "高度摘要", "v360-log-scale-100.png"),
    ):
        shown = _set_scale(cdp, value)
        text = _log_text(cdp)
        texts[str(value)] = text
        shot = os.path.join(ASSETS, name)
        cdp.screenshot(shot)
        print("      档位 {} → 标签「{}」 截图 → {}".format(
            value, shown, os.path.basename(shot)
        ))
        shots[str(value)] = {
            "label": shown,
            "screenshot": name,
            "chars": len(text),
        }

    # 走一圈回到 0 档 —— 内容必须**逐字相同**（证明重绘走缓存，不是重拉）。
    _set_scale(cdp, 50)
    _set_scale(cdp, 100)
    back = _set_scale(cdp, 0)
    back_text = _log_text(cdp)

    code_text = texts.get("0", "")
    natural_text = texts.get("50", "")
    summary_text = texts.get("100", "")

    evidence["render_scale"] = {
        "shots": shots,
        "code_mode_shows_raw_json": '"type"' in code_text,
        "code_mode_shows_arguments": "arguments" in code_text,
        "code_mode_shows_noise_tag": "<environment_context>" in code_text,
        "code_mode_no_emoji": "🧑" not in code_text,
        "natural_mode_has_user_tag": "🧑 用户提问" in natural_text,
        "natural_mode_has_reply_tag": "🤖 Agent 回答" in natural_text,
        "natural_mode_has_think_tag": "💭 思考" in natural_text,
        "natural_mode_has_tool_tag": "🔧 工具调用" in natural_text,
        "natural_mode_hides_arguments": "arguments" not in natural_text,
        "natural_mode_hides_tool_result": TOOL_RESULT_TEXT not in natural_text,
        "natural_mode_strips_noise": "<environment_context>" not in natural_text,
        "natural_mode_keeps_question": NOISE_TAIL in natural_text,
        "summary_mode_has_user_tag": "🧑 用户提问" in summary_text,
        "summary_mode_has_reply_tag": "🤖 Agent 回答" in summary_text,
        "summary_mode_has_tool_tag": "🔧 工具调用" in summary_text,
        "summary_mode_drops_thinking": "💭 思考" not in summary_text,
        "summary_mode_drops_tool_result": (
            TOOL_RESULT_TEXT not in summary_text
        ),
        "round_trip_label": back,
        "round_trip_identical": back_text == code_text,
    }


# ===========================================================================
# 演示二：多面板拖拽与折叠
# ===========================================================================
def _demo_split_panes(
    cdp: _Cdp,
    evidence: Dict[str, Any],
) -> None:
    """演示二：左右拖、上下拖、左侧折叠、刷新后保持。

    :param cdp: CDP 客户端。
    :param evidence: 证据字典（原样修改）。
    """
    print("[5/9] 量拖拽前的几何 …")
    before = cdp.evaluate(_JS_GEOMETRY)
    print("      会话栏宽 {} / 日志宽 {} / 流水高 {}".format(
        (before or {}).get("sessions_rect", {}).get("width"),
        (before or {}).get("stream_rect", {}).get("width"),
        (before or {}).get("audit_rect", {}).get("height"),
    ))

    print("[6/9] 往右拖会话栏分隔条 …")
    _drag(cdp, "#resizer-sessions", 120, 0)
    after_sessions = cdp.evaluate(_JS_GEOMETRY)

    print("[7/9] 往左拖日志分隔条 …")
    _drag(cdp, "#resizer-stream", -140, 0)
    after_stream = cdp.evaluate(_JS_GEOMETRY)

    print("[8/9] 往下拖告警/流水分隔条 …")
    _drag(cdp, "#resizer-audit", 0, 90)
    after_audit = cdp.evaluate(_JS_GEOMETRY)

    shot = os.path.join(ASSETS, "v360-split-panes.png")
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    before_sessions = int((before or {}).get("sessions_rect", {}).get("width") or 0)
    after_sessions_w = int(
        (after_sessions or {}).get("sessions_rect", {}).get("width") or 0
    )
    before_stream = int((before or {}).get("stream_rect", {}).get("width") or 0)
    after_stream_w = int(
        (after_stream or {}).get("stream_rect", {}).get("width") or 0
    )
    before_audit = int((before or {}).get("audit_rect", {}).get("height") or 0)
    after_audit_h = int(
        (after_audit or {}).get("audit_rect", {}).get("height") or 0
    )

    print("      会话栏 {} → {}；日志 {} → {}；流水 {} → {}".format(
        before_sessions, after_sessions_w,
        before_stream, after_stream_w,
        before_audit, after_audit_h,
    ))

    evidence["split_panes"] = {
        "before": before,
        "after_sessions": after_sessions,
        "after_stream": after_stream,
        "after_audit": after_audit,
        "sessions_widened": after_sessions_w > before_sessions,
        "stream_widened": after_stream_w > before_stream,
        "audit_taller": after_audit_h > before_audit,
        "screenshot": os.path.basename(shot),
    }

    print("[9/9] 折叠会话栏 → 侧边条 → 展开 …")
    collapsed = cdp.evaluate(
        "(function(){var b=document.getElementById('sessions-collapse');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    time.sleep(0.8)
    folded = cdp.evaluate(_JS_GEOMETRY)
    folded_shot = os.path.join(ASSETS, "v360-sessions-collapsed.png")
    cdp.screenshot(folded_shot)
    print("      折叠：{} 截图 → {}".format(
        collapsed, os.path.basename(folded_shot)
    ))

    folded_width = int(
        (folded or {}).get("sessions_rect", {}).get("width") or 0
    )
    rail_hidden = (folded or {}).get("rail_hidden")
    rail_count = (folded or {}).get("rail_count")
    # ⚠️ 折叠**不能**把别的面板挤没 ——
    #   左分隔条一旦被 `display: none` 摘出网格流，
    #   后面三格自动补位：告警面板被塞进 8px 的分隔条列，
    #   日志面板也被塞进 8px，中间分隔条反倒是霸占了整列 1fr ——
    #   整页就只剩一条空窄条。所以这里必须量**另外两个面板**。
    folded_alert = int(
        (folded or {}).get("alert_panel_rect", {}).get("width") or 0
    )
    folded_stream = int(
        (folded or {}).get("stream_rect", {}).get("width") or 0
    )

    expanded = cdp.evaluate(
        "(function(){var b=document.getElementById('sessions-expand');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    time.sleep(0.8)
    restored = cdp.evaluate(_JS_GEOMETRY)
    restored_width = int(
        (restored or {}).get("sessions_rect", {}).get("width") or 0
    )

    print("      折叠后宽 {} / 窄条隐藏={} / 会话数={}".format(
        folded_width, rail_hidden, rail_count
    ))
    print("      折叠后告警宽 {} / 日志宽 {}".format(
        folded_alert, folded_stream
    ))
    print("      展开后宽 {}（折叠按钮 {}）".format(
        restored_width, expanded
    ))

    evidence["split_panes"]["collapse"] = {
        "clicked": collapsed,
        "collapsed_width": folded_width,
        "rail_visible": rail_hidden is False,
        "rail_session_count": rail_count,
        "narrow_strip": 0 < folded_width <= 80,
        "expanded_width": restored_width,
        "expanded_ok": restored_width > folded_width,
        "alert_width": folded_alert,
        "stream_width": folded_stream,
        "alert_panel_intact": folded_alert > 200,
        "stream_panel_intact": folded_stream > 200,
        "screenshot": os.path.basename(folded_shot),
    }

    # --- 刷新后布局还在不在 ---
    print("     刷新页面，看比例还在不在 …")
    cdp.send("Page.reload", {"ignoreCache": True})
    time.sleep(6.0)
    reloaded = cdp.evaluate(_JS_GEOMETRY)
    reloaded_sessions = int(
        (reloaded or {}).get("sessions_rect", {}).get("width") or 0
    )
    reloaded_stream = int(
        (reloaded or {}).get("stream_rect", {}).get("width") or 0
    )
    print("      刷新后会话栏宽 {} / 日志宽 {}".format(
        reloaded_sessions, reloaded_stream
    ))
    evidence["split_panes"]["persisted"] = {
        "sessions_width": reloaded_sessions,
        "stream_width": reloaded_stream,
        "sessions_kept": abs(reloaded_sessions - after_sessions_w) <= 4,
        "stream_kept": abs(reloaded_stream - after_stream_w) <= 4,
    }


# ===========================================================================
# 演示三：路径校验与空态
# ===========================================================================
def _demo_path_guard(
    cdp: _Cdp,
    base: str,
    evidence: Dict[str, Any],
) -> None:
    """演示三：不存在的路径被前端拦下 + 空态只给相对路径。

    :param cdp: CDP 客户端。
    :param base: 看板地址。
    :param evidence: 证据字典（原样修改）。
    """
    print("     打开「发现 / 挂载进程」弹窗 …")
    cdp.evaluate(
        "(function(){var b=document.getElementById('attach-button');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    _wait_for(cdp, "!!document.querySelector('.attach__manual')", timeout=30)
    _wait_for(cdp, "!!document.querySelector('.attach__empty')", timeout=30)
    time.sleep(1.0)

    empty_command = str(
        cdp.evaluate(
            "(function(){var n=document.querySelector('.radar-empty__command');"
            "return n?n.textContent:'';})()"
        )
        or ""
    )
    empty_note = str(
        cdp.evaluate(
            "(function(){var n=document.querySelector('.radar-empty__note');"
            "return n?n.textContent:'';})()"
        )
        or ""
    )
    empty_target = _command_target_path(empty_command)
    print("      空态命令：{}".format(empty_command))
    print("      空态提示：{}".format(empty_note))
    print("      重定向目标：{}".format(empty_target))

    empty_shot = os.path.join(ASSETS, "v360-radar-empty.png")
    cdp.screenshot(empty_shot)

    sessions_before = 0
    try:
        sessions_before = len(
            (_get_json(base + "/api/sessions") or {}).get("sessions") or []
        )
    except (urllib.error.URLError, OSError, ValueError):
        sessions_before = 0

    print("     填一个根本不存在的路径，点【接入】…")
    cdp.evaluate(
        "(function(){var i=document.querySelector('.attach__input');"
        "if(!i){return null;}"
        "i.value=" + json.dumps(MISSING_PATH) + ";"
        "i.dispatchEvent(new Event('input',{bubbles:true}));"
        "var b=i.parentNode.querySelector('.btn--primary');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    _wait_for(
        cdp,
        "(function(){var n=document.querySelector('.attach__error');"
        "return n && !n.hidden && n.textContent ? 1 : 0;})()",
        timeout=30,
    )
    time.sleep(0.6)

    error_text = str(
        cdp.evaluate(
            "(function(){var n=document.querySelector('.attach__error');"
            "return n&&!n.hidden?n.textContent:'';})()"
        )
        or ""
    )
    error_color = str(
        cdp.evaluate(
            "(function(){var n=document.querySelector('.attach__error');"
            "return n?getComputedStyle(n).color:'';})()"
        )
        or ""
    )
    print("      红字：{}（颜色 {}）".format(error_text, error_color))

    blocked_shot = os.path.join(ASSETS, "v360-attach-blocked.png")
    cdp.screenshot(blocked_shot)
    print("      截图 → {}".format(blocked_shot))

    sessions_after = 0
    try:
        sessions_after = len(
            (_get_json(base + "/api/sessions") or {}).get("sessions") or []
        )
    except (urllib.error.URLError, OSError, ValueError):
        sessions_after = 0

    backend = {}
    try:
        backend = _get_json(
            base + "/api/path_exists?path=" + urllib.request.quote(
                MISSING_PATH
            )
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("      后端校验接口没响应：{}".format(exc))

    evidence["path_guard"] = {
        "empty_state_command": empty_command,
        "empty_state_target": empty_target,
        "empty_state_note": empty_note,
        "empty_state_uses_relative_path": (
            empty_target == RADAR_RELATIVE_PATH
            and not os.path.isabs(empty_target)
        ),
        "empty_state_says_generate_first": (
            RADAR_MISSING_NOTE in empty_note
        ),
        "empty_state_screenshot": os.path.basename(empty_shot),
        "missing_path": MISSING_PATH,
        "error_text": error_text,
        "error_matches": ATTACH_ERROR_TEXT in error_text,
        "error_color": error_color,
        "sessions_before": sessions_before,
        "sessions_after": sessions_after,
        "nothing_attached": sessions_after == sessions_before,
        "backend_exists_flag": backend.get("exists"),
        "backend_agrees_missing": backend.get("exists") is False,
        "screenshot": os.path.basename(blocked_shot),
    }

    # 把弹窗关掉 —— 不关的话后面的截图全是弹窗。
    cdp.evaluate(
        "(function(){var b=document.querySelector("
        "'#sentinel-modal .modal__close');"
        "if(!b){return 'no-close';}b.click();return 'closed';})()"
    )
    time.sleep(0.6)


# ===========================================================================
# 演示四：流水区问答高亮
# ===========================================================================
def _demo_flow_highlight(
    cdp: _Cdp,
    evidence: Dict[str, Any],
) -> None:
    """演示四：流水区里的用户提问 / Agent 回答是**气泡样式**。

    :param cdp: CDP 客户端。
    :param evidence: 证据字典（原样修改）。
    """
    print("     查流水区里的问答高亮 …")
    _wait_for(
        cdp,
        "document.querySelectorAll('.live-audit__line').length >= 2",
        timeout=30,
    )
    time.sleep(0.8)

    flow = cdp.evaluate(
        """
(function(){
  var rows=document.querySelectorAll('.live-audit__line');
  var users=0, replies=0, plain=0;
  var sample_user='', sample_reply='';
  for(var i=0;i<rows.length;i++){
    var n=rows[i];
    var txt=n.textContent||'';
    if(n.classList.contains('live-audit__line--user')){
      users++;
      if(!sample_user){sample_user=txt;}
    }else if(n.classList.contains('live-audit__line--reply')){
      replies++;
      if(!sample_reply){sample_reply=txt;}
    }else{
      plain++;
    }
  }
  var u=document.querySelector('.live-audit__line--user');
  var r=document.querySelector('.live-audit__line--reply');
  function style(n){
    if(!n){return null;}
    var c=getComputedStyle(n);
    return {border_left:c.borderLeftWidth+' '+c.borderLeftStyle+' '+c.borderLeftColor,
            background:c.backgroundColor};
  }
  return {total:rows.length, users:users, replies:replies, plain:plain,
          sample_user:sample_user, sample_reply:sample_reply,
          user_style:style(u), reply_style:style(r)};
})()
"""
    )
    print("      流水条目：{}（用户 {} / 回答 {} / 其它 {}）".format(
        (flow or {}).get("total"),
        (flow or {}).get("users"),
        (flow or {}).get("replies"),
        (flow or {}).get("plain"),
    ))
    shot = os.path.join(ASSETS, "v360-flow-highlight.png")
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    evidence["flow_highlight"] = {
        "flow": flow,
        "has_user_bubble": int((flow or {}).get("users") or 0) >= 1,
        "has_reply_bubble": int((flow or {}).get("replies") or 0) >= 1,
        "user_text_has_question": USER_GOAL in str(
            (flow or {}).get("sample_user") or ""
        ),
        "reply_text_has_answer": AGENT_REPLY in str(
            (flow or {}).get("sample_reply") or ""
        ),
        "user_has_border": bool(
            ((flow or {}).get("user_style") or {}).get("border_left")
        ),
        "screenshot": os.path.basename(shot),
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
    alert_log = os.path.join(WORK, "sentinel_alert.log")
    for path in (WATCH_LOG, alert_log, MISSING_PATH):
        if os.path.exists(path):
            os.remove(path)
    # ⚠️ 挂载持久化（v3.1.0）——
    #    上一轮演示挂过的会话会被**自动接回来**，
    #    那样「一个会话都没接进来」的断言就不成立了。
    persist = os.path.join(WORK, "attached_sessions.json")
    if os.path.exists(persist):
        os.remove(persist)
        print("      清掉上一轮的挂载持久化记录")

    _prepare_watch_log(WATCH_LOG)
    config = _write_demo_config(
        os.path.join(WORK, "v360-config.yaml"), RADAR_ROOT
    )

    evidence: Dict[str, Any] = {
        "version": "v3.6.0",
        "port": PORT,
        "demo_log": os.path.basename(WATCH_LOG),
        "radar_root": RADAR_ROOT,
        "expected_user_goal": USER_GOAL,
        "expected_agent_reply": AGENT_REPLY,
        "missing_path": MISSING_PATH,
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
            # ⚠️ 必须带 --tail ——
            #   只有常驻监听模式才会建**引擎**、把每条新事件过一遍规则，
            #   规则判 ON_TRACK 的事件才会写成「实时审核流水」行。
            #   不带 --tail 就只有「立即扫描」按钮会临时建一次引擎，
            #   流水区会一直是空的。
            "--tail",
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
    browser_proc = None
    cdp = None
    profile = None
    try:
        print("[1/9] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return 1
        print("      看板已就绪：{}".format(base))

        print("[2/9] 打开浏览器 …")
        browser = _find_browser()
        profile = os.path.join(
            os.environ.get("TEMP", "."),
            "sentinel-v360-cdp-{}".format(int(time.time() * 1000)),
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
        time.sleep(6.0)

        # ⚠️ 现在才往被监控日志里**追加**内容 ——
        #   看板的常驻监听是从**文件末尾**开始跟的，
        #   启动前写好的行它看不到，也就不会过规则、不出流水。
        #   追加之后，这一批行对引擎来说全是「新事件」。
        print("     往被监控日志追加 {} 行演示事件 …".format(
            _append_watch_log(WATCH_LOG)
        ))
        time.sleep(3.0)

        _demo_render_scale(cdp, evidence)
        _demo_split_panes(cdp, evidence)
        _demo_flow_highlight(cdp, evidence)
        _demo_path_guard(cdp, base, evidence)

        print("     写证据 …")
        report = os.path.join(ASSETS, "v360-evidence.json")
        os.makedirs(ASSETS, exist_ok=True)
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("     证据 → {}".format(report))

        scale = evidence.get("render_scale", {})
        panes = evidence.get("split_panes", {})
        collapse = panes.get("collapse", {})
        persisted = panes.get("persisted", {})
        flow = evidence.get("flow_highlight", {})
        guard = evidence.get("path_guard", {})

        checks = [
            ("0 档显示原始 JSON", scale.get("code_mode_shows_raw_json")),
            ("0 档保留 arguments", scale.get("code_mode_shows_arguments")),
            ("0 档没有自然语言标签", scale.get("code_mode_no_emoji")),
            ("50 档有 🧑 用户提问", scale.get("natural_mode_has_user_tag")),
            ("50 档有 🤖 Agent 回答", scale.get("natural_mode_has_reply_tag")),
            ("50 档有 💭 思考", scale.get("natural_mode_has_think_tag")),
            ("50 档藏起 arguments", scale.get("natural_mode_hides_arguments")),
            ("50 档藏起工具结果", scale.get("natural_mode_hides_tool_result")),
            ("50 档挖掉噪声段落", scale.get("natural_mode_strips_noise")),
            ("50 档保留真实问题", scale.get("natural_mode_keeps_question")),
            ("100 档没有 💭 思考", scale.get("summary_mode_drops_thinking")),
            ("100 档保留问题+回答", (
                scale.get("summary_mode_has_user_tag")
                and scale.get("summary_mode_has_reply_tag")
            )),
            ("100 档保留工具调用", scale.get("summary_mode_has_tool_tag")),
            ("切档位不丢内容", scale.get("round_trip_identical")),
            ("会话栏能拖宽", panes.get("sessions_widened")),
            ("日志面板能拖宽", panes.get("stream_widened")),
            ("流水区能拖高", panes.get("audit_taller")),
            ("会话栏能折叠成窄条", collapse.get("narrow_strip")),
            ("窄条显示会话数量", collapse.get("rail_visible")),
            ("折叠后告警面板还在", collapse.get("alert_panel_intact")),
            ("折叠后日志面板还在", collapse.get("stream_panel_intact")),
            ("能再展开", collapse.get("expanded_ok")),
            ("刷新后比例还在", (
                persisted.get("sessions_kept")
                and persisted.get("stream_kept")
            )),
            ("流水有用户气泡", flow.get("has_user_bubble")),
            ("流水有回答气泡", flow.get("has_reply_bubble")),
            ("气泡里是真实原话", (
                flow.get("user_text_has_question")
                and flow.get("reply_text_has_answer")
            )),
            ("空态只给相对路径", guard.get("empty_state_uses_relative_path")),
            ("空态提示先生成", guard.get("empty_state_says_generate_first")),
            ("不存在路径被红字拦下", guard.get("error_matches")),
            ("拦下时没有接入会话", guard.get("nothing_attached")),
            ("后端也说不存在", guard.get("backend_agrees_missing")),
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
        # 浏览器 profile 目录是**自己**建的，就得**自己**收走 ——
        # 不收的话每跑一次演示就往系统临时目录里丢一个几十 MB 的
        # Chrome 用户目录，跑几次就把临时目录塞满了。
        if profile and os.path.isdir(profile):
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
