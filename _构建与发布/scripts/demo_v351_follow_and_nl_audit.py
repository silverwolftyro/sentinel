"""v3.5.1 验收演示 —— 日志跟随锁定 · 自然语言流水 · 空态引导。

它做的事**每一步都是真的**
==========================

1. **自然语言流水 + 三色** ——
   造一份**真的** MCode 格式日志（正常动作 + 一次死循环 + 一条红线命令），
   起**真的**看板（``src.gui.server``），让**真的规则引擎**判定，
   再用 CDP 驱动 Edge 拍照 ——
   绿（正常）/ 橘黄（死循环）/ 红（红线）三色必须同时出现在画面上。

2. **日志跟随锁定** ——
   把日志面板**真的往上滚**，再往被监控日志里**真的追加**新行，
   然后回读 ``scrollTop`` ——
   它必须**原地不动**（没被拽回底部），
   【↓ 回到最新】按钮必须**亮着**；
   点它之后必须平滑滚到底并**恢复跟随**。

3. **雷达空态引导** ——
   把雷达的扫描根指向一个**空目录**（所以真的扫不到任何 Agent），
   打开「发现 / 挂载进程」弹窗 ——
   里面必须出现那行**一字不差**的引导文案、可执行命令和【复制命令】按钮；
   点复制按钮，按钮文字必须变成「已复制 ✓」。

⚠️ 演示用的配置文件写在 ``logs/v351-demo/`` 下，
   **不会**写回 ``config/config.yaml``；
   它只改了一件事：雷达的扫描根。

用法::

    python _构建与发布/scripts/demo_v351_follow_and_nl_audit.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
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

#: 任务书要求的引导文案（**一字不差**）。
EMPTY_TITLE = "未发现正在运行的 Agent。若要开始监控，请在终端中运行："

#: 演示日志里的思考正文（长于 40 字，好让前端看到**截断**）。
THINKING_TEXT = (
    "我先确认一下这个模块的边界条件，再决定要不要改它的签名；"
    "如果只改内部实现，对外的调用方应该完全不受影响。"
)

#: 演示日志里的第二次思考正文。
THINKING_TEXT_2 = (
    "回归跑完了，失败的那两条都指向同一个文件；"
    "先把失败用例单独拎出来复现，确认不是偶发。"
)


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
def _write_demo_log(path: str) -> str:
    """写一份**真的** MCode 格式演示日志。

    ⚠️ 三段内容是**故意**这么排的：

    * 开头两个 ``thinking`` 与若干正常工具调用 → 引擎判 ``ON_TRACK``
      → 前端渲染成**绿色**的「Agent 正在思考：…」「正常读取：…」；
    * **连续三次**读同一个文件 → 死循环检测器命中 → ``LOOP``
      → 前端渲染成**橘黄**的「警告：连续 3 次读取同一个文件…」；
    * 一条 ``rm -rf /`` → 红线级 → ``DANGEROUS``
      → 前端渲染成**红色**的「危险！拦截到高危命令：…」。

    ⚠️ **不放** ``user_message`` ——
    放了会被自动锚定接管，流水区会多出「任务目标」的提示，干扰画面。

    :param path: 目标路径。
    :return: 路径字符串。
    """
    lines: List[Dict[str, Any]] = [
        {"type": "thinking", "text": THINKING_TEXT},
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
        # ↑ 连续第三次读同一个文件 → LOOP（橘黄）。
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Bash",
                "arguments": {"command": "rm -rf /"},
            },
        },
        # ↑ 红线命令 → DANGEROUS（红色）。
        {"type": "thinking", "text": THINKING_TEXT_2},
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
        {
            "type": "tool_call",
            "tool_call": {
                "name": "Bash",
                "arguments": {"command": "ruff check ."},
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


def _append_log_lines(path: str, count: int) -> int:
    """往被监控日志里**真的追加**若干行（模拟 Agent 正在写）。

    :param path: 日志路径。
    :param count: 追加多少行。
    :return: 实际追加的行数。
    """
    payload = [
        json.dumps({
            "type": "tool_call",
            "tool_call": {
                "name": "Read",
                "arguments": {
                    "file_path": "src/module_{:02d}.py".format(index % 7),
                },
            },
        }, ensure_ascii=False)
        for index in range(count)
    ]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(payload) + "\n")
        handle.flush()
    return len(payload)


def _write_demo_config(path: str, scan_root: str) -> str:
    """写一份演示专用配置：只改**雷达的扫描根**。

    ⚠️ 用的是 ``config/config.example.yaml`` 当底稿，
    别的配置项（规则、阈值、通知通道）**一个都不动**；
    这份配置只存在于 ``logs/v351-demo/`` 下，
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

        self._ws = websocket.create_connection(ws_url, timeout=30)
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
# 三段演示
# ===========================================================================
def _demo_audit(
    cdp: _Cdp,
    base: str,
    work: str,
    evidence: Dict[str, Any],
) -> None:
    """演示一：自然语言流水 + 三色。

    :param cdp: CDP 客户端。
    :param base: 看板地址。
    :param work: 工作目录。
    :param evidence: 证据字典（原样修改）。
    """
    print("[2/6] 触发扫描，等引擎真的判完 …")
    try:
        result = _post_json(base + "/api/scan")
        print("      扫描返回：{}".format(
            result.get("ok") if isinstance(result, dict) else result
        ))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("      扫描接口没响应：{}".format(exc))

    print("[3/6] 等三色流水都出来 …")
    _wait_for(
        cdp,
        "document.querySelectorAll('.live-audit__line--ok').length >= 3",
        timeout=40,
    )
    _wait_for(
        cdp,
        "document.querySelectorAll('.live-audit__line--warn').length >= 1",
        timeout=40,
    )
    _wait_for(
        cdp,
        "document.querySelectorAll('.live-audit__line--danger').length >= 1",
        timeout=40,
    )
    ok_count = cdp.evaluate(
        "document.querySelectorAll('.live-audit__line--ok').length"
    )
    warn_count = cdp.evaluate(
        "document.querySelectorAll('.live-audit__line--warn').length"
    )
    danger_count = cdp.evaluate(
        "document.querySelectorAll('.live-audit__line--danger').length"
    )
    lines = cdp.evaluate(
        "Array.from(document.querySelectorAll('.live-audit__line'))"
        ".map(function(n){return n.textContent;})"
    )
    print("      绿色 {} 条 / 橘黄 {} 条 / 红色 {} 条".format(
        ok_count, warn_count, danger_count
    ))
    if isinstance(lines, list):
        for item in lines:
            print("        · {}".format(item))

    shot = os.path.join(
        ROOT, "_管理文档", "文档与报告", "assets", "v351-nl-audit.png"
    )
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    evidence["audit_stream"] = {
        "ok_lines": cdp.evaluate(
            "document.querySelectorAll('.live-audit__line--ok').length"
        ),
        "warn_lines": cdp.evaluate(
            "document.querySelectorAll('.live-audit__line--warn').length"
        ),
        "danger_lines": cdp.evaluate(
            "document.querySelectorAll('.live-audit__line--danger').length"
        ),
        "sentences": lines,
        "screenshot": os.path.basename(shot),
    }


#: 读取日志面板滚动位置的 JS。
_JS_BOX = (
    "(function(){var b=document.querySelector('#stream-panel .panel__body');"
    "if(!b){return null;}"
    "return {top:b.scrollTop,height:b.scrollHeight,client:b.clientHeight,"
    "gap:b.scrollHeight-b.scrollTop-b.clientHeight};})()"
)


def _box_value(box, key: str, fallback: float) -> float:
    """从滚动位置字典里取一个数值。

    ⚠️ **0 是有效值** —— 回到底部时 ``gap`` 就是 ``0``。
    所以这里必须显式判断 ``None``，
    写成 ``box.get(key) or fallback`` 会把 ``0`` 变成 ``fallback``，
    明明已经回到底部却报「没回去」。

    :param box: :data:`_JS_BOX` 的返回值。
    :param key: 要取的键名。
    :param fallback: 取不到时的兜底值。
    :return: 取到的数值。
    """
    if not isinstance(box, dict):
        return fallback
    value = box.get(key)
    if value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _demo_follow(
    cdp: _Cdp,
    base: str,
    work: str,
    watch: str,
    evidence: Dict[str, Any],
) -> None:
    """演示二：日志跟随锁定。

    :param cdp: CDP 客户端。
    :param base: 看板地址。
    :param work: 工作目录。
    :param watch: 被监控日志路径。
    :param evidence: 证据字典（原样修改）。
    """
    print("[4/6] 往日志里追加行，让面板能滚 …")
    _append_log_lines(watch, 60)
    time.sleep(4.0)

    print("      把日志面板往上滚（模拟用户滚轮向上）…")
    cdp.evaluate(
        "(function(){var b=document.querySelector("
        "'#stream-panel .panel__body');"
        "if(!b){return 'no-box';}"
        "b.scrollTop=0;"
        "b.dispatchEvent(new Event('scroll'));"
        "return 'scrolled';})()"
    )
    time.sleep(1.5)
    before = cdp.evaluate(_JS_BOX)
    button = cdp.evaluate(
        "(function(){var b=document.getElementById('log-follow-button');"
        "if(!b){return null;}"
        "return {hidden:b.hidden,visible:"
        "b.classList.contains('log-follow--visible'),"
        "text:b.textContent};})()"
    )
    print("      滚动位置：{}".format(before))
    print("      【回到最新】按钮：{}".format(button))

    print("      追加新日志行（看它会不会把用户拽回底部）…")
    _append_log_lines(watch, 12)
    time.sleep(6.0)
    after = cdp.evaluate(_JS_BOX)
    print("      追加后滚动位置：{}".format(after))

    shot = os.path.join(
        ROOT, "_管理文档", "文档与报告", "assets", "v351-log-follow.png"
    )
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    before_top = _box_value(before, "top", 0.0)
    after_top = _box_value(after, "top", 0.0)
    after_gap = _box_value(after, "gap", 999.0)
    stayed = (
        isinstance(before, dict)
        and isinstance(after, dict)
        and abs(after_top - before_top) <= 60
        and after_gap > 50
    )
    print("      没被拽回底部：{}".format(stayed))

    print("      点【回到最新】…")
    cdp.evaluate(
        "(function(){var b=document.getElementById('log-follow-button');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    time.sleep(2.5)
    restored = cdp.evaluate(_JS_BOX)
    restored_button = cdp.evaluate(
        "(function(){var b=document.getElementById('log-follow-button');"
        "if(!b){return null;}"
        "return {hidden:b.hidden,visible:"
        "b.classList.contains('log-follow--visible')};})()"
    )
    print("      点击后滚动位置：{}".format(restored))
    print("      点击后按钮：{}".format(restored_button))
    back_gap = _box_value(restored, "gap", 999.0)
    print("      已回到底部：{}".format(back_gap < 50))

    evidence["log_follow"] = {
        "scroll_before_new_lines": before,
        "scroll_after_new_lines": after,
        "button_while_detached": button,
        "stayed_while_detached": stayed,
        "scroll_after_click": restored,
        "button_after_click": restored_button,
        "returned_to_bottom": back_gap < 50,
        "screenshot": os.path.basename(shot),
    }


def _demo_radar_empty(
    cdp: _Cdp,
    base: str,
    evidence: Dict[str, Any],
) -> None:
    """演示三：雷达空态引导。

    :param cdp: CDP 客户端。
    :param base: 看板地址。
    :param evidence: 证据字典（原样修改）。
    """
    print("[5/6] 打开「发现 / 挂载进程」弹窗 …")
    cdp.evaluate(
        "(function(){var b=document.getElementById('attach-button');"
        "if(!b){return 'no-button';}b.click();return 'clicked';})()"
    )
    found = _wait_for(
        cdp,
        "!!document.querySelector('.radar-empty__command')",
        timeout=25,
    )
    time.sleep(1.0)
    title = cdp.evaluate(
        "(function(){var n=document.querySelector('.radar-empty__title');"
        "return n?n.textContent:null;})()"
    )
    command = cdp.evaluate(
        "(function(){var n=document.querySelector('.radar-empty__command');"
        "return n?n.textContent:null;})()"
    )
    print("      引导标题：{}".format(title))
    print("      引导命令：{}".format(command))

    print("      点【复制命令】…")
    cdp.evaluate(
        "(function(){window.__sentinel_clip=null;var b="
        "document.querySelector('.radar-empty__actions .btn');"
        "if(!b){return 'no-copy';}b.click();return 'clicked';})()"
    )
    # 回读剪贴板 —— 「按钮说复制成功了」得跟真剪贴板对得上。
    cdp.evaluate(
        "(function(){try{navigator.clipboard.readText().then("
        "function(t){window.__sentinel_clip=t;},"
        "function(e){window.__sentinel_clip='读取失败:'+e;});}"
        "catch(e){window.__sentinel_clip='读取失败:'+e;}"
        "return true;})()"
    )
    copy_text = ""
    for _ in range(12):
        time.sleep(0.35)
        copy_text = cdp.evaluate(
            "(function(){var b=document.querySelector("
            "'.radar-empty__actions .btn');"
            "return b?b.textContent:null;})()"
        ) or ""
        if "已复制" in copy_text or "失败" in copy_text:
            break
    clip = cdp.evaluate("window.__sentinel_clip")
    print("      复制按钮文字：{}".format(copy_text))
    print("      剪贴板内容：{}".format(clip))

    shot = os.path.join(
        ROOT, "_管理文档", "文档与报告", "assets", "v351-radar-empty.png"
    )
    cdp.screenshot(shot)
    print("      截图 → {}".format(shot))

    evidence["radar_empty"] = {
        "guide_found": bool(found),
        "title": title,
        "title_matches": title == EMPTY_TITLE,
        "command": command,
        "copy_button_text_after_click": copy_text,
        "copy_succeeded": "已复制" in (copy_text or ""),
        "clipboard_content": clip,
        "clipboard_matches_command": clip == command,
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
    port = 8151
    cdp_port = 9361
    base = "http://127.0.0.1:{}".format(port)
    work = os.path.join(ROOT, "logs", "v351-demo")
    os.makedirs(work, exist_ok=True)
    watch = os.path.join(work, "mcode.jsonl")
    alert_log = os.path.join(work, "sentinel_alert.log")
    empty_root = os.path.join(work, "empty-radar-root")
    os.makedirs(empty_root, exist_ok=True)
    for path in (watch, alert_log):
        if os.path.exists(path):
            os.remove(path)
    _write_demo_log(watch)
    config = _write_demo_config(
        os.path.join(work, "v351-config.yaml"), empty_root
    )

    evidence: Dict[str, Any] = {
        "version": "v3.5.1",
        "port": port,
        "demo_log": os.path.basename(watch),
    }

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    server_log = open(
        os.path.join(work, "server.log"), "w", encoding="utf-8"
    )
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "src.gui.server",
            "--watch", watch,
            "--alert-log", alert_log,
            "--config", config,
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
    browser = None
    browser_proc = None
    cdp = None
    try:
        print("[1/6] 等看板起来 …")
        if not _wait_http(base + "/api/state"):
            print("      看板没起来 —— 证据不完整")
            return 1
        print("      看板已就绪：{}".format(base))

        print("      打开浏览器 …")
        browser = _find_browser()
        profile = os.path.join(
            os.environ.get("TEMP", "."),
            "sentinel-v351-cdp-{}".format(int(time.time() * 1000)),
        )
        browser_proc = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--remote-debugging-port={}".format(cdp_port),
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
                    "http://127.0.0.1:{}/json/list".format(cdp_port),
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
        # ⚠️ 无头浏览器的页面**默认没有焦点** ——
        # 而 ``navigator.clipboard.writeText`` 遇到失焦会直接拒绝。
        # 这一句让页面「以为自己有焦点」，剪贴板才肯收。
        try:
            cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except (KeyError, TypeError) as exc:
            print("      焦点模拟没成功（不影响其它演示）：{}".format(exc))
        cdp.send("Page.navigate", {"url": base + "/"})
        time.sleep(4.0)
        # 剪贴板权限是**按 origin** 授权的 ——
        # 必须等页面跳到那个 origin 之后再给。
        try:
            cdp.send("Browser.grantPermissions", {
                "origin": base,
                "permissions": [
                    "clipboardReadWrite",
                    "clipboardSanitizedWrite",
                ],
            })
        except (KeyError, TypeError) as exc:
            print("      剪贴板授权没成功（不影响其它演示）：{}".format(exc))
        time.sleep(2.0)

        _demo_audit(cdp, base, work, evidence)
        _demo_follow(cdp, base, work, watch, evidence)
        _demo_radar_empty(cdp, base, evidence)

        print("[6/6] 写证据 …")
        report = os.path.join(
            ROOT,
            "_管理文档",
            "文档与报告",
            "assets",
            "v351-follow-nl-audit-evidence.json",
        )
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        print("      证据 → {}".format(report))

        ok = (
            (evidence.get("audit_stream", {}).get("ok_lines") or 0) >= 1
            and (evidence.get("audit_stream", {}).get("warn_lines") or 0) >= 1
            and (evidence.get("audit_stream", {}).get("danger_lines") or 0) >= 1
            and evidence.get("log_follow", {}).get("stayed_while_detached")
            and evidence.get("log_follow", {}).get("returned_to_bottom")
            and evidence.get("radar_empty", {}).get("title_matches")
        )
        print("")
        print("验收结论：{}".format("全部通过 ✓" if ok else "有项目没通过 ✗"))
        return 0 if ok else 1
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


if __name__ == "__main__":
    sys.exit(main())
