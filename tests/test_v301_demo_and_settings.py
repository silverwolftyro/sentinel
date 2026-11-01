"""v3.0.1 回归：演示退出 · 设置页分栏 · 雷达配置写回。

三块：

1. **演示模式能真的退出**（任务一）——
   横幅上有【退出演示，开始真实扫描】按钮；点它之后
   演示进程会停下来，页面给出**能直接复制**的真命令；
2. **设置页是左右分栏**（任务二）——
   左边 240px 导航，右边全宽内容，卡片不再自己滚，
   窄屏下导航变顶部横向 Tab；
3. **雷达扫描根目录能写回配置**（任务三）——
   设置页填的路径要能落到 ``config.yaml`` 的 ``radar.scan_roots``。
"""

from __future__ import annotations

import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from src.console import (
    DEMO_KEEP_OPEN_LINES,
    DEMO_REMINDER_INTERVAL,
    DEMO_REMINDER_LINE,
    DEMO_REMINDER_TIMES,
)
from src.gui.server import (
    DEMO_EXIT_INSTRUCTIONS,
    _collect_config_updates,
    create_app,
)

#: 项目根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    """读仓库里的一个文件。

    :param parts: 相对项目根的路径片段。
    :return: 文件文本。
    """
    path = os.path.join(ROOT, *parts)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _make_app(tmp_path, **kwargs):
    """造一个最小可用的看板应用。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    return create_app(
        str(watch), str(alert), start_watcher=False, **kwargs
    )


# ---------------------------------------------------------------------------
# 1. 演示模式退出演示
# ---------------------------------------------------------------------------
def test_横幅有退出演示按钮(tmp_path):
    """红色横幅最右侧要有那个大按钮。"""
    app = _make_app(tmp_path, demo_mode=True)
    with TestClient(app) as client:
        html = client.get("/").text

    assert "demo-exit-button" in html
    assert "退出演示" in html


def test_按钮文案完整(tmp_path):
    """按钮要写清楚「退出演示，开始真实扫描」—— 不是「关闭」。"""
    app = _make_app(tmp_path, demo_mode=True)
    with TestClient(app) as client:
        html = client.get("/").text

    assert "退出演示，开始真实扫描" in html


def test_真实模式没有退出演示按钮(tmp_path):
    """真实监控时不该出现「退出演示」—— 没什么可退的。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text

    assert "demo-exit-button" not in html


def test_页面里有退出演示后的引导页(tmp_path):
    """引导页必须**已经在页面里**。

    ⚠️ 服务端一停，浏览器就再也拿不到任何数据 ——
    引导内容要是等 JS 去 fetch，就永远显示不出来。
    """
    app = _make_app(tmp_path, demo_mode=True)
    with TestClient(app) as client:
        html = client.get("/").text

    assert "demo-exit-panel" in html
    assert "demo-exit-code" in html


def test_退出演示接口在真实模式报错(tmp_path):
    """真实模式点这个按钮是误操作 —— 必须明说，不能假装成功。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.post("/api/demo/exit").json()

    assert payload["ok"] is False
    assert "演示模式" in payload["error"]


def test_退出演示接口给出可复制的命令(tmp_path):
    """接口要返回**能直接复制粘贴**的真命令。"""
    app = _make_app(tmp_path, demo_mode=True)
    with TestClient(app) as client:
        payload = client.post("/api/demo/exit").json()

    assert payload["ok"] is True
    assert payload["instructions"]
    text = "\n".join(payload["instructions"])
    # 必须覆盖三种真实用法：接管 Agent / 只扫描 / 不开界面。
    assert "--scan" in text
    assert "--no-gui" in text


def test_退出演示指令常量非空():
    """引导指令不能是空的 —— 空了就等于没引导。

    ⚠️ 里面**允许**有空行（分段用），但不能有「只有空白」的行。
    """
    assert DEMO_EXIT_INSTRUCTIONS
    lines = [str(item) for item in DEMO_EXIT_INSTRUCTIONS]
    assert any(item.strip() for item in lines)
    for item in lines:
        # 命令行前面**要**有缩进（复制出来才像命令），
        # 但行尾不能有空白 —— 那是复制不干净的来源。
        assert item == item.rstrip()
        assert not (item and not item.strip())


def test_退出演示指令是纯文本():
    """前端用 ``textContent`` 显示 —— 不能夹带 Markdown 标记。"""
    text = "\n".join(str(item) for item in DEMO_EXIT_INSTRUCTIONS)

    assert "**" not in text
    assert "`" not in text


def test_退出演示会真的停掉服务器(tmp_path):
    """有句柄时，接口要安排停服（``should_exit=True``）。"""
    from src.gui import server as server_module

    class _FakeServer:
        """假的 uvicorn Server。"""

        def __init__(self) -> None:
            self.should_exit = False

    fake = _FakeServer()
    original = server_module._DEMO_SHUTDOWN.get("server")
    server_module._DEMO_SHUTDOWN["server"] = fake
    try:
        app = _make_app(tmp_path, demo_mode=True)
        with TestClient(app) as client:
            payload = client.post("/api/demo/exit").json()

        assert payload["stopped"] is True
        # 延迟停服 —— 给响应留出发送的时间。
        deadline = time.time() + 5
        while time.time() < deadline and not fake.should_exit:
            time.sleep(0.05)
        assert fake.should_exit is True
    finally:
        server_module._DEMO_SHUTDOWN["server"] = original


def test_终端有别关窗口的醒目块():
    """终端必须有一块醒目的「不要关闭此窗口」。"""
    text = "\n".join(DEMO_KEEP_OPEN_LINES)
    assert "不要关闭此窗口" in text
    assert "看板" in text


def test_终端提醒会重复若干次():
    """「反复提示」= 间隔 + 次数都要有，不能只喊一遍。"""
    assert DEMO_REMINDER_INTERVAL > 0
    assert DEMO_REMINDER_TIMES >= 3
    assert "不要关闭此窗口" in DEMO_REMINDER_LINE


# ---------------------------------------------------------------------------
# 2. 设置页：左右分栏（v3.0.2 改成 MPA —— 布局在基座模板里）
# ---------------------------------------------------------------------------
def test_设置页有左右分栏结构():
    """左边导航 + 右边内容区。"""
    html = _read("src", "gui", "templates", "settings_base.html")

    assert "settings-layout" in html
    assert "settings-nav" in html
    assert "settings-content" in html


def test_导航有五个菜单项():
    """任务书点名了四个：模型路由 / 通知通道 / L1 规则 / 挂载管理。

    v3.2.0 又加了第 ⑤ 项「关于系统」；
    v3.7.0 再加第 ③ 项「流水线控制」——
    「审不审、审多狠」独立成页，不塞进「模型路由」。
    """
    html = _read("src", "gui", "templates", "settings_base.html")

    assert html.count("settings-nav__item") >= 5
    for label in (
        "模型路由",
        "通知通道",
        "流水线控制",
        "L1 规则",
        "挂载管理",
        "关于系统",
    ):
        assert label in html


def test_导航项是真链接且指向子页面():
    """v3.0.2：左侧菜单必须是**真链接**，URL 要真的变。

    ⚠️ 之前是 ``<button data-target="xxx">`` + 前端只切显示 ——
    看着像导航，实际上 URL 从头到尾都是 ``/settings``，
    浏览器后退键也不工作。现在每个菜单项都是独立页面。
    """
    html = _read("src", "gui", "templates", "settings_base.html")

    targets = set(re.findall(r'data-nav="(/settings/[^"]+)"', html))
    assert targets == {
        "/settings/llm",
        "/settings/webhook",
        # v3.7.0：第 ③ 项「流水线控制」—— L1/L2/L3 开关 + 拦截策略 + 呈现档位。
        "/settings/pipeline",
        "/settings/rules",
        "/settings/sessions",
        # v3.2.0：第 ⑤ 项「关于系统」—— 运行时长 / 内存 / 版本号。
        "/settings/system",
    }
    # 旧的「同页切换」标记必须**清干净**。
    # ⚠️ 注释里提到过 ``data-target``（说明为什么改掉），
    #    所以先把 HTML 注释剥掉再查。
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert not re.search(r"<[^>]*data-target=", markup)
    assert not re.search(r"<[^>]*data-section=", markup)


def test_左侧导航固定240像素():
    """左边那一列就是 240px。"""
    html = _read("src", "gui", "templates", "settings_base.html")

    assert "grid-template-columns: 240px minmax(0, 1fr)" in html


def test_右边内容区占满剩余宽度():
    """内容区是 ``minmax(0, 1fr)`` —— 等价于任务书说的 ``flex: 1``。"""
    html = _read("src", "gui", "templates", "settings_base.html")

    assert "minmax(0, 1fr)" in html


def test_卡片不再自己滚():
    """``.panel__body`` 的 ``overflow-y: auto`` 必须被覆盖掉。

    ⚠️ 不覆盖的话，卡片里会藏一个小滚动条，内容被框在里头 ——
    看上去就是「挤成一条、打不开」。
    """
    html = _read("src", "gui", "templates", "settings_base.html")

    match = re.search(
        r"\.settings-page > \.panel__body[^}]*\{([^}]*)\}", html
    )
    assert match, "找不到 .settings-page > .panel__body 的覆盖规则"
    body = match.group(1)
    assert "overflow: visible" in body
    assert "overflow-y: auto" not in body
    assert "flex: none" in body


def test_窄屏下导航变顶部横向():
    """<768px 时导航横向排列、能左右滑。

    ⚠️ v3.9.0：断点 760px → **768px** ——
    与看板（static/dashboard.css）对齐，两页必须同一个断点。
    """
    html = _read("src", "gui", "templates", "settings_base.html")

    assert "@media (max-width: 768px)" in html
    match = re.search(r"@media \(max-width: 768px\)\s*\{(.*)\n\}\n", html, re.S)
    assert match
    block = match.group(1)
    assert "settings-nav" in block
    assert "flex" in block
    assert "overflow-x: auto" in block


def test_每个子页面都遵守页面契约():
    """v3.0.2：设置页拆成了 6 个脚本，每个都得暴露页面契约。"""
    for name in (
        "settings_index.js",
        "settings_llm.js",
        "settings_webhook.js",
        "settings_rules.js",
        "settings_sessions.js",
    ):
        script = _read("src", "gui", "static", name)
        assert "SentinelPage" in script, name
        assert "init" in script, name
        assert "destroy" in script, name


# ---------------------------------------------------------------------------
# 3. 雷达扫描根目录：配置写回
# ---------------------------------------------------------------------------
def test_扫描根目录能翻译成配置路径():
    """设置页的表单要翻译成 ``radar.scan_roots``。"""
    updates = _collect_config_updates(
        {"radar": {"scan_roots": [".codex", "D:\\项目", "  "]}}
    )

    assert ("radar.scan_roots", [".codex", "D:\\项目"]) in updates


def test_没有雷达段就不产生更新():
    """没填就不该写。"""
    updates = _collect_config_updates({"llm": {"model": "x"}})

    assert not [item for item in updates if item[0].startswith("radar")]


def test_配置接口返回雷达段(tmp_path):
    """设置页要能读回当前配置。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/config").json()

    assert "radar" in payload
    assert "radar_roots" in payload


def test_发现接口返回新字段(tmp_path, monkeypatch):
    """/api/discover 的每条结果都要带工作区 / 会话 ID / 时间。"""
    from src import radar as radar_module

    class _FakeSession:
        """假的会话对象。"""

        path = os.path.join("C:", "tmp", "rollout-a.jsonl")
        agent = "codex"
        agent_label = "Codex"
        cwd = "D:\\work"
        label = "[Codex] D:\\work"
        size = 10
        mtime = 1000.0
        age = 1.0
        active = True
        workspace = "D:\\work"
        workspace_source = "cwd"
        session_id = "sid-1"
        first_time = 999.0
        primary = True

        def to_dict(self):
            """转成字典。"""
            return {
                "path": self.path,
                "agent": self.agent,
                "agent_label": self.agent_label,
                "cwd": self.cwd,
                "label": self.label,
                "size": self.size,
                "mtime": self.mtime,
                "age": self.age,
                "active": self.active,
                "workspace": self.workspace,
                "workspace_source": self.workspace_source,
                "session_id": self.session_id,
                "first_time": self.first_time,
                "primary": self.primary,
            }

    monkeypatch.setattr(
        radar_module,
        "scan_agent_sessions",
        lambda **kwargs: [_FakeSession()],
    )

    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/discover").json()

    assert payload["ok"] is True
    item = payload["sessions"][0]
    assert item["workspace"] == "D:\\work"
    assert item["session_id"] == "sid-1"
    assert item["first_time"] == 999.0
    assert item["primary"] is True
    assert payload["active_count"] == 1


def test_设置页有扫描根目录输入框():
    """设置页要能填扫描根目录（v3.0.2 在「挂载管理」子页面里）。"""
    html = _read("src", "gui", "templates", "settings_sessions.html")

    assert "radar-scan-roots" in html


def test_设置页脚本会收集扫描根目录():
    """表单值要真的被收集起来。"""
    script = _read("src", "gui", "static", "settings_sessions.js")

    assert "collectPayload" in script
    assert "scan_roots" in script


def test_配置模板里有雷达段():
    """``config.example.yaml`` 必须带 ``radar.scan_roots``。"""
    text = _read("config", "config.example.yaml")

    assert "radar:" in text
    assert "scan_roots:" in text
    assert ".codex" in text
    assert ".claude" in text


# ---------------------------------------------------------------------------
# 4. 雷达列表的前端：默认只显示活跃文件
# ---------------------------------------------------------------------------
def test_雷达默认只显示活跃的主日志():
    """v3.0.2：过滤搬到**服务端**做了。

    ``/api/discover`` 默认 ``active_only=1`` —— 只返回「正在写入」的文件，
    而且服务端**连静默文件都不读**（这一路只花几十毫秒）。
    前端只要按这个口径请求就行。
    """
    script = _read("src", "gui", "static", "dashboard.js")

    assert "active_only" in script
    assert "fetchDiscover" in script
    assert "attach-show-all" in script


def test_雷达列表展示会话ID与时间():
    """列表里要能看到会话 ID 与首条消息时间。"""
    script = _read("src", "gui", "static", "dashboard.js")

    assert "item.session_id" in script
    assert "item.first_time" in script
    assert "formatStamp" in script


def test_雷达列表不再显示未知工作区():
    """雷达那段渲染里不能自己造一个「未知工作区」。

    ⚠️ 只查雷达渲染那一段 —— 看板别处的「未知工作区」是
    「当前监控目标」面板的兜底文案，与本任务无关。
    """
    script = _read("src", "gui", "static", "dashboard.js")

    start = script.find("function renderRows(rows)")
    assert start > 0, "找不到雷达列表的渲染函数"
    block = script[start:start + 4000]
    assert "未知工作区" not in block


@pytest.mark.parametrize(
    "path",
    [
        ("src", "gui", "static", "dashboard.js"),
        ("src", "gui", "static", "settings_common.js"),
        ("src", "gui", "static", "settings_index.js"),
        ("src", "gui", "static", "settings_llm.js"),
        ("src", "gui", "static", "settings_webhook.js"),
        ("src", "gui", "static", "settings_rules.js"),
        ("src", "gui", "static", "settings_sessions.js"),
    ],
)
def test_前端脚本语法正确(path):
    """``node --check`` 跑一遍（没装 node 就跳过）。"""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node，跳过语法检查")
    result = subprocess.run(
        [node, "--check", os.path.join(ROOT, *path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
