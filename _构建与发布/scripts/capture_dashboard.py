"""看板截图 —— 用 CDP 驱动 Edge 给本地看板拍照。

用途是给 ``_管理文档/文档与报告/assets/`` 里的界面实拍出图。

**为什么不用 Edge 自带的 ``--screenshot``**：看板用 SSE 保持长连接，
无头模式下的虚拟时钟会被长连接卡住，``--screenshot`` 往往截不到
渲染完成的那一帧。CDP 可以显式等待、可选可交互元素、并能回读 DOM
断言 —— 所以这里走 CDP。

用法::

    python _构建与发布/scripts/capture_dashboard.py --url http://127.0.0.1:8000/
    python _构建与发布/scripts/capture_dashboard.py --out 截图.png

⚠️ **出正式实拍请加 ``--scale 2``** ——
无头 Chromium 在 1x 栅格化下画中文合成粗体时会糊成两半，
看着像「重影」，那是**截图伪影**，页面本身没有这个问题。
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

#: Edge 可执行文件候选路径。
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
)


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

#: 默认出图位置（界面实拍归档在管理文档下）。
DEFAULT_OUTPUT = os.path.join(
    PROJECT_ROOT, "_管理文档", "文档与报告", "assets", "dashboard.png"
)


def _find_browser() -> str:
    """找一个可用的 Chromium 内核浏览器。"""
    for path in EDGE_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("找不到 Edge / Chrome")


def _safe(value: object) -> str:
    """把回读的内容变成**控制台打得出来**的字符串。

    ⚠️ Windows 控制台默认 GBK —— 页面里有个 ``⚠️`` 或 emoji
    就能把脚本炸掉（UnicodeEncodeError）。
    """
    text = str(value if value is not None else "")
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
    return text


def _http_json(url: str, timeout: float = 10.0) -> dict:
    """GET 一个 JSON 接口。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _Cdp:
    """极简 CDP 客户端。"""

    def __init__(self, ws_url: str) -> None:
        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._counter = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        """发一条 CDP 命令并等它的回包。"""
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

    def close(self) -> None:
        """关闭连接。"""
        self._ws.close()


def main() -> int:
    """截图主流程。"""
    parser = argparse.ArgumentParser(description="哨兵看板截图")
    parser.add_argument("--url", default="http://127.0.0.1:8000/")
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help="截图输出路径，默认写到管理文档的 assets 目录",
    )
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help=(
            "设备像素比 —— 出图用 2 或 3 可以拿到高清实拍。"
            "默认 1 时，无头 Chromium 的合成粗体在 1x 栅格化下"
            "会把中文粗体画成两半（看着像重影），"
            "那是**截图伪影**不是页面缺陷 —— 出正式实拍请用 --scale 2"
        ),
    )
    parser.add_argument("--wait", type=float, default=5.0)
    parser.add_argument(
        "--click",
        default="",
        help=(
            "截图前点一下某个元素（CSS 选择器）—— "
            "多路会话看板要切到具体那一路时才看得到它的数据"
        ),
    )
    parser.add_argument(
        "--click-wait",
        type=float,
        default=2.0,
        help="点击后等多久再截图（秒）",
    )
    parser.add_argument(
        "--scroll-to",
        default="",
        help=(
            "截图前把某个元素滚动到可视区（CSS 选择器）—— "
            "告警列表是滚动容器，深处的卡片默认看不到"
        ),
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="顺便回读 DOM 摘要（卡片数、按钮状态等）",
    )
    parser.add_argument(
        "--eval",
        default="",
        help=(
            "截图前执行一段 JS 并回读它的返回值 —— "
            "用来核对弹窗里的文字、尺寸等细节"
        ),
    )
    args = parser.parse_args()

    # ⚠️ 控制台默认可能是 GBK —— 后面那行「已保存：」含中文，
    # 直接 print 会炸（UnicodeEncodeError）或者写出乱码。
    # 脚本的输出会被验收脚本**原样收进 JSON 报告**，乱码会污染数据。
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is None:
            continue
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError) as exc:
            print(
                "切换控制台编码失败：{}: {}".format(
                    type(exc).__name__, exc
                ),
                file=sys.stderr,
            )

    browser = _find_browser()
    # 每次用全新 profile —— 避免浏览器磁盘缓存拿到旧版静态资源
    profile = os.path.join(
        os.environ.get("TEMP", "."),
        f"sentinel-cdp-{int(time.time() * 1000)}",
    )
    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={profile}",
            f"--window-size={args.width},{args.height}",
            "--hide-scrollbars",
            "--no-first-run",
            "--disable-gpu",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                targets = _http_json(
                    f"http://127.0.0.1:{args.port}/json/list"
                )
            except (OSError, ValueError, http.client.HTTPException):
                # 浏览器还没起来，端口没监听是**预期内**的正常情况。
                continue
            for item in targets:
                if item.get("type") == "page":
                    target = item
                    break
            if target:
                break
        if not target:
            print("未能连接 DevTools", file=sys.stderr)
            return 1

        cdp = _Cdp(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": args.width,
            "height": args.height,
            "deviceScaleFactor": args.scale,
            "mobile": False,
        })
        cdp.send("Page.navigate", {"url": args.url})
        time.sleep(args.wait)

        if args.scroll_to:
            scrolled = cdp.send("Runtime.evaluate", {
                "expression": (
                    "(function(){var el=document.querySelector("
                    + json.dumps(args.scroll_to)
                    + ");"
                    "if(!el){return 'not-found';}"
                    "el.scrollIntoView({block:'center'});"
                    "return 'scrolled';})()"
                ),
                "returnByValue": True,
            })
            print(
                "滚动：",
                scrolled.get("result", {}).get("result", {}).get("value"),
            )
            time.sleep(1.0)

        if args.click:
            # 多个选择器用逗号隔开 —— 按顺序点，每个点完等一下。
            # 例：``--click '#attach-button,#attach-show-all'``。
            for selector in [
                item.strip() for item in args.click.split(",") if item.strip()
            ]:
                clicked = cdp.send("Runtime.evaluate", {
                    "expression": (
                        "(function(){var el=document.querySelector("
                        + json.dumps(selector)
                        + ");"
                        "if(!el){return 'not-found';}"
                        "el.click();return 'clicked';})()"
                    ),
                    "returnByValue": True,
                })
                print(
                    "点击：",
                    clicked.get("result", {}).get("result", {}).get("value"),
                    selector,
                )
                time.sleep(args.click_wait)

        if args.probe:
            probe = cdp.send("Runtime.evaluate", {
                "expression": (
                    "JSON.stringify({"
                    "cards: document.querySelectorAll('.alert-card').length,"
                    "redline: document.querySelectorAll("
                    "'.alert-card--redline').length,"
                    "btns: Array.from("
                    "document.querySelectorAll('.alert-actions .btn'))"
                    ".map(function(b){return b.textContent"
                    " + (b.disabled ? '(禁用)' : '(可用)')}),"
                    "chip: (document.getElementById('guardian-chip')"
                    "||{}).textContent})"
                ),
                "returnByValue": True,
            })
            value = probe.get("result", {}).get("result", {}).get("value")
            print("DOM:", value)

        if args.eval:
            probe = cdp.send("Runtime.evaluate", {
                "expression": args.eval,
                "returnByValue": True,
            })
            value = probe.get("result", {}).get("result", {}).get("value")
            # ⚠️ 控制台可能是 GBK —— 回读的内容里有 emoji 会直接炸。
            print("EVAL:", _safe(value))

        shot = cdp.send("Page.captureScreenshot", {"format": "png"})
        data = shot.get("result", {}).get("data")
        if not data:
            print("截图失败", file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "wb") as handle:
            handle.write(base64.b64decode(data))
        print("已保存：", os.path.abspath(args.out))
        cdp.close()
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
