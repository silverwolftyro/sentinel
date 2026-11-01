"""v3.3.2 回归：雷达的活跃进程感知 · 挂载管理页的快捷重定向命令。

背景
====

用户在自己的终端里跑着一个**真实的** MCode 任务，看板却抓不到它。
根因有两个：

1. **雷达扫的是文件** —— Agent 没把输出重定向成文件，雷达自然看不见；
2. **用户不知道该去哪儿找日志路径** —— 看板和终端彻底脱节。

v3.3.2 用两条路回应：

* **任务一**：雷达除了扫文件，还扫**进程** —— 发现「本机有活跃 Agent
  进程，却对不上正在监控的日志」时，在看板左侧给出高亮提示与引导；
* **任务二**：挂载管理页给出**针对当前这台机器**的「快捷重定向命令」，
  让用户下一次能直接用 ``> log.jsonl`` 的方式启动任务，从而被雷达捕获。

本文件钉住：

1. **进程识别** —— 必须「进程名 + 命令行特征」两层同时满足，
   否则满机的 Node 进程都会被算进来；
2. **「有没有被监控」的判定** —— 三条信号，从确定到推断；
3. **接口契约** —— ``/api/process_activity`` 的字段与降级行为；
4. **界面文案** —— 提示长什么样、点了之后去哪儿；
5. **快捷重定向命令** —— 生成的命令必须带 JSONL 开关与重定向。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.gui.server import _match_monitored, create_app
from src.procscan import (
    AGENT_HOST_IMAGES,
    AGENT_PROCESS_MARKERS,
    AgentProcess,
    detect_agent_process,
    psutil_available,
    scan_agent_processes,
)

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "gui" / "static"
TEMPLATE_DIR = ROOT / "src" / "gui" / "templates"


@pytest.fixture(autouse=True)
def _清活跃缓存():
    """每个用例都从「没缓存」开始。

    「活跃进程感知」带 8 秒缓存 —— 不清的话前一个用例的结果
    会直接被下一个用例读走，用例之间互相干扰。
    """
    from src.gui import server

    server._activity_cache["at"] = 0.0
    server._activity_cache["data"] = {}
    yield
    server._activity_cache["at"] = 0.0
    server._activity_cache["data"] = {}


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    path = ROOT.joinpath(*parts)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 一、进程识别：两层信号缺一不可
# ---------------------------------------------------------------------------
def test_命令行能认出三种_agent():
    """包名/命令名 → Agent 键。"""
    assert detect_agent_process(
        "node.exe C:\\npm\\node_modules\\@minimax-ai\\code\\cli.js --continue"
    ) == "mcode"
    assert detect_agent_process(
        "node.exe /opt/codex/node_modules/@openai/codex/bin/codex.js exec"
    ) == "codex"
    assert detect_agent_process(
        "node.exe /opt/claude/node_modules/@anthropic-ai/claude-code/cli.js"
    ) == "claude"


def test_路径分隔符两种写法都认得出():
    """``\\`` 与 ``/`` 都要认得出 —— 换个安装方式不该就认不出来了。"""
    assert detect_agent_process(
        "node.exe C:\\npm\\node_modules\\@minimax-ai\\code\\cli.js"
    ) == "mcode"
    assert detect_agent_process(
        "node.exe C:/npm/node_modules/@minimax-ai/code/cli.js"
    ) == "mcode"


def test_认不出_非_agent_进程():
    """跟 Agent 无关的 Node 进程**不能**被算进来。

    ⚠️ 本机实测踩过的坑：``npx rea-agents@6.4.0 mcp`` 跑在
    ``.minimax-code\\runtime`` 目录下，一度被误判成 Agent ——
    那会让提示变成「狼来了」，用户点进来发现是假的就再也不信了。
    """
    assert detect_agent_process(
        "node.exe C:\\Users\\x\\.minimax-code\\runtime\\node.exe "
        "C:\\npm\\npx-cli.js -y rea-agents@6.4.0 mcp"
    ) == ""
    assert detect_agent_process("node.exe --input-type=module --eval 1") == ""
    assert detect_agent_process("") == ""


def test_宿主进程名清单覆盖_node():
    """Node 是各家 Agent CLI 的宿主 —— 进程名清单必须收它。"""
    assert "node" in AGENT_HOST_IMAGES
    assert "node.exe" in AGENT_HOST_IMAGES


def test_每个_agent都有对应特征():
    """每种 Agent 都要有自己的特征 —— 不能只认一种。"""
    assert set(AGENT_PROCESS_MARKERS) == {"mcode", "codex", "claude"}
    for key, markers in AGENT_PROCESS_MARKERS.items():
        assert markers, "Agent {} 没有任何特征".format(key)


def test_扫描结果满足两层不变量():
    """真扫一遍系统：每一条都必须「进程名对得上 + 是 Agent」。"""
    if not psutil_available():
        return
    found = scan_agent_processes()
    for item in found:
        assert item.name.lower() in AGENT_HOST_IMAGES
        assert item.agent in ("mcode", "codex", "claude")
        assert detect_agent_process(item.cmdline) == item.agent


def test_扫描有上限():
    """扫描条数必须有上限 —— 不能一次吐出几百个进程。"""
    if not psutil_available():
        return
    assert len(scan_agent_processes(limit=2)) <= 2


def test_扫描失败时降级为空列表():
    """psutil 缺失 → 返回空列表，**不抛异常**（架构红线 R5）。"""
    from src import procscan

    original = procscan.psutil_module
    procscan.psutil_module = lambda: None
    try:
        assert procscan.scan_agent_processes() == []
    finally:
        procscan.psutil_module = original


# ---------------------------------------------------------------------------
# 二、「有没有被监控」的三条信号
# ---------------------------------------------------------------------------
def _proc(**kwargs) -> AgentProcess:
    """造一个 :class:`AgentProcess` 测试替身。

    :param kwargs: 覆盖默认字段。
    :return: 进程对象。
    """
    base = {
        "pid": 1234,
        "agent": "mcode",
        "exe": "C:\\node.exe",
        "name": "node.exe",
        "cwd": "D:\\项目\\demo",
        "cmdline": "node.exe cli.js --continue",
    }
    base.update(kwargs)
    return AgentProcess(**base)


def test_命令行带日志路径算被监控():
    """信号一：命令行里带着正在监控的日志路径 —— **确定**是它。"""
    proc = _proc(cmdline="node.exe cli.js > D:\\logs\\session.jsonl")
    rows = [{
        "path": "D:\\logs\\session.jsonl",
        "agent": "mcode",
        "workspace": "",
        "recent": True,
        "under_scan_root": True,
    }]
    assert _match_monitored(proc, rows) == "cmdline"


def test_工作目录对得上算被监控():
    """信号二：同一个项目 —— **确定**是它。"""
    proc = _proc(cwd="D:\\项目\\demo")
    rows = [{
        "path": "C:\\other\\a.jsonl",
        "agent": "codex",
        "workspace": "D:\\项目\\demo",
        "recent": False,
        "under_scan_root": False,
    }]
    assert _match_monitored(proc, rows) == "cwd"


def test_近期写过且在扫描目录内算被监控():
    """信号三：**推断** —— 同类型 Agent + 日志刚在雷达目录里被写过。"""
    proc = _proc(agent="mcode")
    rows = [{
        "path": "C:\\Users\\x\\.minimax\\v2\\sessions\\a.jsonl",
        "agent": "mcode",
        "workspace": "",
        "recent": True,
        "under_scan_root": True,
    }]
    assert _match_monitored(proc, rows) == "recent"


def test_推断信号的三道门槛缺一不可():
    """推断必须**同时**满足：同类型 + 近期写过 + 落在雷达扫描目录内。"""
    proc = _proc(agent="mcode")

    # ① 不是近期写的 → 不算。
    assert _match_monitored(proc, [{
        "path": "a.jsonl", "agent": "mcode", "workspace": "",
        "recent": False, "under_scan_root": True,
    }]) == ""

    # ② 不在雷达扫描目录内 → 不算。
    assert _match_monitored(proc, [{
        "path": "a.jsonl", "agent": "mcode", "workspace": "",
        "recent": True, "under_scan_root": False,
    }]) == ""

    # ③ 不是同一种 Agent → 不算。
    assert _match_monitored(proc, [{
        "path": "a.jsonl", "agent": "codex", "workspace": "",
        "recent": True, "under_scan_root": True,
    }]) == ""


def test_对不上任何信号就是没被监控():
    """什么都不匹配 → 空串（界面据此弹出提示）。"""
    proc = _proc(agent="mcode", cwd="D:\\项目\\别的")
    rows = [{
        "path": "C:\\x\\a.jsonl", "agent": "codex", "workspace": "D:\\项目\\别的2",
        "recent": False, "under_scan_root": False,
    }]
    assert _match_monitored(proc, rows) == ""


# ---------------------------------------------------------------------------
# 三、接口契约
# ---------------------------------------------------------------------------
def _app(tmp_path):
    """造一个最小可用的看板应用。

    :param tmp_path: pytest 的临时目录。
    :return: FastAPI 应用。
    """
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    return create_app(str(watch), str(alert), start_watcher=False)


def test_活跃进程接口字段齐全(tmp_path):
    """``/api/process_activity`` 必须把该说的都说清楚。"""
    client = TestClient(_app(tmp_path))
    data = client.get("/api/process_activity").json()

    assert data["ok"] is True
    assert "available" in data
    assert isinstance(data["active"], list)
    assert isinstance(data["unmonitored"], list)
    assert data["active_count"] == len(data["active"])
    assert data["unmonitored_count"] == len(data["unmonitored"])
    assert isinstance(data["by_agent"], dict)
    # 重定向命令的默认落盘位置 —— 界面要用它预填路径。
    assert str(data.get("capture_path") or "").endswith("capture.jsonl")


def test_接口带缓存(tmp_path):
    """枚举进程不便宜 —— 短时间内连问两次应该走缓存。"""
    client = TestClient(_app(tmp_path))
    first = client.get("/api/process_activity").json()
    second = client.get("/api/process_activity").json()
    assert first["active_count"] == second["active_count"]


def test_探测失败时接口不报错(tmp_path, monkeypatch):
    """psutil 缺失 → ``available=False``，接口照常 200（红线 R5）。"""
    from src import procscan

    original = procscan.psutil_module
    procscan.psutil_module = lambda: None
    try:
        client = TestClient(_app(tmp_path))
        response = client.get("/api/process_activity")
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False
        assert data["unmonitored_count"] == 0
    finally:
        procscan.psutil_module = original


# ---------------------------------------------------------------------------
# 四、界面：提示长什么样、点了之后去哪儿
# ---------------------------------------------------------------------------
def test_看板左侧有提示容器():
    """提示框必须挂在左侧「＋ 发现/挂载进程」下面。"""
    html = _read("src", "gui", "templates", "dashboard.html")
    assert 'id="radar-alert"' in html
    assert 'id="radar-alert-title"' in html
    # 引导文案要告诉用户下一步干什么。
    assert "手动填入其输出文件路径" in html
    assert "重定向重启" in html


def test_提示框默认可聚焦():
    """提示框要能被键盘操作 —— 没有它鼠标用户以外的人用不了。"""
    html = _read("src", "gui", "templates", "dashboard.html")
    block = html.split('id="radar-alert"', 1)[1][:400]
    assert 'role="button"' in block
    assert 'tabindex="0"' in block


def test_提示样式是橘黄而不是红色():
    """橘黄 = 「你可能没接上」；红色在哨兵的语言里是「出事了」。"""
    css = _read("src", "gui", "static", "dashboard.css")
    assert ".radar-alert" in css
    assert "--drift-soft" in css


def test_前端会拉接口并渲染文案():
    """看板必须自己去拉这个接口 —— 不然提示永远不出现。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert "/api/process_activity" in js
    assert "radarAlertText" in js
    assert "loadProcessActivity" in js
    # 轮询：Agent 进程是一直在变的，静态页面不行。
    assert "setInterval(loadProcessActivity" in js


def test_提示文案的两种形态():
    """单一种 Agent 用产品名；混了好几种写总数 + 分项。

    ⚠️ 句式写成**常量** —— 这样「用户到底会看到哪句话」
    在源码里就有**一处**可以钉住的地方。
    """
    js = _read("src", "gui", "static", "dashboard.js")
    # 单一种：用户认识「MCode」，不写「MCode 1 个 进程」这种拗口的话。
    assert (
        "检测到本机有 {count} 个活跃的 {agent} 进程，未发现可读日志。"
        in js
    )
    # 好几种：写总数 + 分项。
    assert (
        "检测到本机有 {count} 个活跃的 Agent 进程（{detail}），未发现可读日志。"
        in js
    )
    assert "RADAR_ALERT_SINGLE" in js
    assert "RADAR_ALERT_MULTI" in js


def test_提示文案不带多余的个数():
    """单一种 Agent 时**不能**把个数再塞进产品名里。

    ⚠️ 踩过的坑：拼成「MCode 1 个 进程」——
    读起来像卡带。个数已经在前面说过了。
    """
    js = _read("src", "gui", "static", "dashboard.js")
    # 单一分支：{agent} 只能被**裸的产品名**替换。
    single = js.split("if (keys.length === 1) {", 1)[1].split(
        "var parts = keys.map", 1
    )[0]
    assert "agentLabel(keys[0])" in single
    assert "byAgent[" not in single
    # 多种分支：分项才带个数。
    multi = js.split("var parts = keys.map", 1)[1].split("});", 1)[0]
    assert "byAgent[key]" in multi
    assert '" 个"' in multi


def test_点提示直接进手动填路径():
    """点提示 → 打开挂载面板并**聚焦**手动路径输入框。"""
    js = _read("src", "gui", "static", "dashboard.js")
    assert "openAttachModal(true)" in js
    assert "focusManual" in js
    assert "manualInput.focus()" in js


# ---------------------------------------------------------------------------
# 五、快捷重定向命令
# ---------------------------------------------------------------------------
def test_设置页有快捷重定向命令块():
    """挂载管理页必须给出这一块。"""
    html = _read("src", "gui", "templates", "settings_sessions.html")
    assert 'id="capture-command"' in html
    assert 'id="capture-command-text"' in html
    assert 'id="capture-copy-command"' in html
    assert 'id="capture-agent"' in html


def test_命令块带着当前机器的路径():
    """命令要**针对当前用户环境** —— 路径由服务端渲染进去。"""
    html = _read("src", "gui", "templates", "settings_sessions.html")
    assert "radar_capture_path" in html
    assert "radar_workdir" in html


def test_三种_agent的命令都带重定向():
    """生成的命令必须把输出重定向成文件 —— 这是「快捷」的关键。"""
    js = _read("src", "gui", "static", "settings_sessions.js")
    assert "CAPTURE_COMMANDS" in js
    for key in ("mcode", "codex", "claude"):
        assert "{}:".format(key) in js
    assert js.count('> "{capture}"') == 3


def test_命令都带_jsonl_开关():
    """少了 JSONL 开关，重定向出来的就是纯文本 —— 哨兵解析不了。"""
    js = _read("src", "gui", "static", "settings_sessions.js")
    assert "--output-format stream-json" in js
    assert "--json" in js


def test_命令模板变量齐全():
    """模板里要留出工作目录、任务、落盘路径三个占位符。"""
    js = _read("src", "gui", "static", "settings_sessions.js")
    assert "{cwd}" in js
    assert "{task}" in js
    assert "{capture}" in js


def test_页面把捕获路径显示出来():
    """用户得知道输出会落到哪个文件 —— 不然挂载时没法填。"""
    html = _read("src", "gui", "templates", "settings_sessions.html")
    assert 'id="capture-path-text"' in html
    js = _read("src", "gui", "static", "settings_sessions.js")
    assert "capture-path-text" in js
