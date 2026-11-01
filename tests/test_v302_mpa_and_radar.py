"""v3.0.2 回归：多级设置页（MPA）· 精准雷达 · 启动菜单。

三块：

1. **多级设置页**（任务一）——
   ``/settings`` 是干净的菜单列表，四个子页面各自独立，
   左侧菜单是**真链接**（URL 会真的变）；
2. **精准雷达**（任务二）——
   默认只返回「正在写入」的会话，``.codex`` 降优先级，
   ``.minimax/v2/sessions`` 与 ``.dsh/sessions`` 都在清单里，
   DeepSeek 的 zstd 压缩日志**能读出**工作区 / 会话 ID / 时间；
3. **启动菜单**（任务二）——
   无参数启动时终端里打印三个交互式选项，
   默认选项是「开启雷达」而不是演示。
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from src.console import (
    START_MENU_CHOICES,
    START_MENU_DEFAULT,
    START_MENU_LINES,
    print_start_menu,
    read_start_choice,
)
from src.gui.server import create_app
from src.radar import (
    DEFAULT_SCAN_ROOTS,
    describe_session,
    extract_session_id,
    is_compressed_log,
    resolve_scan_roots,
    scan_agent_sessions,
)

#: 项目根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 设置页的五条路由（v3.0.2）。
SETTINGS_ROUTES = (
    "/settings",
    "/settings/llm",
    "/settings/webhook",
    "/settings/rules",
    "/settings/sessions",
)


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


def _write_zstd(path, lines):
    """写一个 zstd 压缩的 JSONL 文件。

    :param path: 目标路径。
    :param lines: 行列表（字符串）。
    :return: 写好的路径。
    """
    payload = "\n".join(lines).encode("utf-8")
    try:
        from compression import zstd
    except ImportError:  # pragma: no cover —— 老 Python 上跳过
        pytest.skip("当前 Python 没有 compression.zstd")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstd.compress(payload))
    return path


# ---------------------------------------------------------------------------
# 1. 多级设置页（MPA）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("route", SETTINGS_ROUTES)
def test_设置页五条路由都能打开(tmp_path, route):
    """五个页面都得真的能访问 —— 不能 404。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get(route)
    assert response.status_code == 200, route


def test_设置主页是干净的菜单列表(tmp_path):
    """``/settings`` 只有菜单卡片墙，**没有**四个功能的表单。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/settings").text

    # 四个入口都在
    assert "/settings/llm" in html
    assert "/settings/webhook" in html
    assert "/settings/rules" in html
    assert "/settings/sessions" in html
    # 但主页**不该**塞进任何功能的表单
    assert "radar-scan-roots" not in html
    assert "dangerous-commands" not in html
    assert "add-channel-button" not in html


def test_模型路由页只有模型路由(tmp_path):
    """``/settings/llm`` 右侧只有模型路由那一个功能。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/settings/llm").text

    assert "设置 · 模型路由" in html
    # 别的功能不该出现在这一页
    assert "radar-scan-roots" not in html
    assert "add-channel-button" not in html


def test_通知通道页只有通知通道(tmp_path):
    """``/settings/webhook`` 右侧只有通知通道。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/settings/webhook").text

    assert "add-channel-button" in html
    assert "radar-scan-roots" not in html
    assert "dangerous-commands" not in html


def test_挂载管理页有雷达根目录与一键复制命令(tmp_path):
    """``/settings/sessions`` 里有雷达配置**和**可复制的命令。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/settings/sessions").text

    assert "radar-scan-roots" in html
    assert "radar-launch-command" in html
    assert "radar-copy-command" in html
    # 别处的功能不该串进来
    assert "add-channel-button" not in html
    assert "dangerous-commands" not in html


def test_左侧菜单是真链接(tmp_path):
    """每个导航项都得是 ``<a href>`` —— 按钮不会改 URL。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/settings").text

    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    links = re.findall(r'<a[^>]*href="(/settings/[^"]+)"', markup)
    for route in (
        "/settings/llm",
        "/settings/webhook",
        "/settings/rules",
        "/settings/sessions",
    ):
        assert route in links, route


def test_看板不渲染设置页内容(tmp_path):
    """主看板只负责监控 —— 不该塞设置表单。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text

    assert "radar-scan-roots" not in html
    assert "add-channel-button" not in html


def test_路由器认识四条子路由():
    """``router.js`` 必须知道这四条 —— 否则点菜单会整页刷新。"""
    script = _read("src", "gui", "static", "router.js")

    for route in (
        "/settings/llm",
        "/settings/webhook",
        "/settings/rules",
        "/settings/sessions",
    ):
        assert route in script, route


# ---------------------------------------------------------------------------
# 2. 精准雷达
# ---------------------------------------------------------------------------
def test_默认清单把_codex_排在最后():
    """``.codex`` 里全是历史死文件 —— 必须让位。"""
    assert DEFAULT_SCAN_ROOTS[-1] == ".codex"


def test_默认清单覆盖_minimax_与_dsh():
    """用户实际在跑的两个目录都必须在清单里。"""
    assert ".minimax/v2/sessions" in DEFAULT_SCAN_ROOTS
    assert ".dsh/sessions" in DEFAULT_SCAN_ROOTS
    # 排在 ``.codex`` 前面
    assert DEFAULT_SCAN_ROOTS.index(".minimax/v2/sessions") < (
        DEFAULT_SCAN_ROOTS.index(".codex")
    )
    assert DEFAULT_SCAN_ROOTS.index(".dsh/sessions") < (
        DEFAULT_SCAN_ROOTS.index(".codex")
    )


def test_清单顺序决定优先级(tmp_path):
    """排在前面的根优先级更高。"""
    roots = resolve_scan_roots(
        [".minimax/v2/sessions", ".dsh/sessions", ".codex"],
        home=str(tmp_path),
    )

    assert roots[0]["priority"] > roots[1]["priority"]
    assert roots[1]["priority"] > roots[2]["priority"]


def test_dsh目录被认成_deepseek(tmp_path):
    """``.dsh`` 是 DeepSeek harness 的家目录。"""
    roots = resolve_scan_roots([".dsh/sessions"], home=str(tmp_path))

    assert roots[0]["agent"] == "dsh"
    assert roots[0]["label"] == "DeepSeek"


def test_压缩日志能被识别():
    """zstd 文件必须被标出来 —— 它不能接入实时审查。"""
    assert is_compressed_log("session.v4.jsonl.zstd") is True
    assert is_compressed_log("messages.jsonl") is False


def test_zstd日志能读出工作区会话ID与时间(tmp_path):
    """DeepSeek 的会话头是压缩的 —— 哨兵得能读出来。

    真实首行长这样：
    ``{"type":"session","version":4,"id":"session-<uuid>",
    "createdAt":…,"cwd":"D:\\\\项目",…}``
    """
    path = _write_zstd(
        tmp_path / "sess" / "session-abc" / "session.v4.jsonl.zstd",
        [
            json.dumps(
                {
                    "type": "session",
                    "version": 4,
                    "id": "session-11111111-2222-3333-4444-555555555555",
                    "createdAt": 1792600830272,
                    "cwd": "D:\\项目",
                },
                ensure_ascii=False,
            ),
            json.dumps({"type": "note"}, ensure_ascii=False),
        ],
    )

    described = describe_session(str(path))

    assert described is not None
    assert described.agent == "dsh"
    assert described.cwd == "D:\\项目"
    assert described.workspace_source == "cwd"
    assert described.session_id == "11111111-2222-3333-4444-555555555555"
    assert described.first_time > 0
    assert described.compressed is True
    assert described.attachable is False
    assert described.primary is True


def test_zstd会话ID从父目录取(tmp_path):
    """会话头读不到 ID 时，父目录名 ``session-<uuid>`` 也要认。"""
    path = _write_zstd(
        tmp_path / "sess" / "session-99999999-8888-7777-6666-555555555555"
        / "session.v3.jsonl.zstd",
        [json.dumps({"type": "note"})],
    )

    assert (
        extract_session_id(str(path), [])
        == "99999999-8888-7777-6666-555555555555"
    )


def test_同一会话只留最新的一份(tmp_path):
    """DeepSeek 同一个会话目录里可能同时有 v3 与 v4 —— 只留一份。"""
    base = tmp_path / "sess" / "session-12345678-1234-1234-1234-123456789012"
    _write_zstd(
        base / "session.v3.jsonl.zstd",
        [json.dumps({"type": "session", "id": "session-12345678-1234-1234-1234-123456789012"})],
    )
    newer = _write_zstd(
        base / "session.v4.jsonl.zstd",
        [json.dumps({"type": "session", "id": "session-12345678-1234-1234-1234-123456789012"})],
    )
    # v3 更旧 —— 留 v4。
    old = time.time() - 3600
    os.utime(base / "session.v3.jsonl.zstd", (old, old))
    os.utime(newer, (time.time(), time.time()))

    found = scan_agent_sessions(
        roots=[
            {
                "agent": "dsh",
                "label": "DeepSeek",
                "path": str(tmp_path / "sess"),
                "priority": 1,
            }
        ],
        limit=10,
    )

    assert len(found) == 1
    assert found[0].path.endswith("session.v4.jsonl.zstd")


def test_只返回正在写入的(tmp_path):
    """``active_only=True`` 时，静默文件一个都不返回。"""
    fresh = tmp_path / "s" / "messages.jsonl"
    fresh.parent.mkdir(parents=True)
    fresh.write_text('{"type":"thinking"}\n', encoding="utf-8")
    stale = tmp_path / "s" / "old.jsonl"
    stale.write_text('{"type":"thinking"}\n', encoding="utf-8")
    old = time.time() - 10 * 3600
    os.utime(stale, (old, old))

    roots = [
        {"agent": "mcode", "label": "MCode", "path": str(tmp_path / "s")},
    ]

    everything = scan_agent_sessions(roots=roots, limit=10)
    only_active = scan_agent_sessions(roots=roots, limit=10, active_only=True)

    assert len(everything) == 2
    assert len(only_active) == 1
    assert only_active[0].active is True


def test_每个根都有机会露面(tmp_path):
    """一个根的历史文件再多，也不能把别的根整个挤掉。"""
    for index in range(5):
        target = tmp_path / ".codex" / f"r{index}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"type":"thinking"}\n', encoding="utf-8")

    dsh = tmp_path / ".dsh" / "session.v4.jsonl.zstd"
    _write_zstd(dsh, [json.dumps({"type": "session", "id": "session-abc"})])

    found = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path / ".codex"),
                "priority": 1,
            },
            {
                "agent": "dsh",
                "label": "DeepSeek",
                "path": str(tmp_path / ".dsh"),
                "priority": 5,
            },
        ],
        limit=10,
    )

    agents = {item.agent for item in found}
    assert "codex" in agents
    assert "dsh" in agents


def test_发现接口默认只给活跃的(tmp_path, monkeypatch):
    """``/api/discover`` 默认 ``active_only=1``。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        default = client.get("/api/discover").json()
        full = client.get("/api/discover?active_only=0").json()

    assert default["ok"] is True
    assert full["ok"] is True
    assert "cwd" in default


def test_发现接口列出当前工作区(tmp_path):
    """任务书要求「明确列出当前的 cwd 工作区」。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/discover").json()

    assert payload["cwd"]
    assert os.path.isabs(payload["cwd"])


def test_压缩日志拒绝接入(tmp_path):
    """zstd 不能被「接入」—— 跟读器按字节偏移走，压缩流读不了。"""
    path = _write_zstd(
        tmp_path / "sess" / "session.v4.jsonl.zstd",
        [json.dumps({"type": "session"})],
    )

    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/attach", json={"path": str(path)})

    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "zstd" in payload["error"]


# ---------------------------------------------------------------------------
# 3. 启动菜单
# ---------------------------------------------------------------------------
def test_菜单有且只有三个选项():
    """任务书原文：三个交互式选项。"""
    text = "\n".join(START_MENU_LINES)

    assert "[1] 启动演示模式 (体验界面)" in text
    assert "[2] 开启雷达 (扫描已有的 Agent 日志)" in text
    assert "[3] 接管并启动新的 Agent" in text
    assert START_MENU_CHOICES == ("1", "2", "3")


def test_菜单默认选项是雷达():
    """默认给「真实的东西」比默认给「假的」诚实。"""
    assert START_MENU_DEFAULT == "2"


def test_非交互环境用默认值且不阻塞(monkeypatch):
    """没有终端可输入时不能干等着 —— 直接用默认值。"""
    monkeypatch.setattr("sys.stdin", None)

    assert read_start_choice(START_MENU_DEFAULT) == "2"


def test_管道输入会被认出来():
    """``echo 1 | sentinel.exe`` 也该管用 —— 不只是 TTY。"""
    import io

    assert read_start_choice("2", io.StringIO("1\n")) == "1"
    assert read_start_choice("2", io.StringIO("3\n")) == "3"
    # 空输入 → 默认
    assert read_start_choice("2", io.StringIO("")) == "2"


def test_带BOM的输入也能认():
    """Windows 重定向常带 UTF-8 BOM —— 不去掉就等于没写。"""
    import io

    assert read_start_choice("2", io.StringIO("\ufeff1\n")) == "1"


def test_菜单能打印到流(capsys):
    """菜单要真的打印出来。"""
    print_start_menu()

    assert "[2] 开启雷达" in capsys.readouterr().out


def test_雷达模式有别关窗口的提醒():
    """雷达模式下终端要反复提醒「别关窗口」。"""
    from src.console import RADAR_KEEP_OPEN_LINES, RADAR_REMINDER_LINE

    text = "\n".join(RADAR_KEEP_OPEN_LINES)
    assert "不要关闭此窗口" in text
    assert "雷达" in text
    assert "不要关闭此窗口" in RADAR_REMINDER_LINE


def test_入口无参数走菜单(monkeypatch):
    """入口脚本的「无参数」分支必须先问用户。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sentinel_entry_v302",
        os.path.join(ROOT, "_构建与发布", "scripts", "sentinel_entry.py"),
    )
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    captured = {}

    def _fake_server_main(argv=None):
        captured["argv"] = list(argv or [])
        return 0

    from src.gui import server

    monkeypatch.setattr(server, "main", _fake_server_main)
    monkeypatch.setattr(entry, "_startup_webhook_probe", lambda: None)
    monkeypatch.setattr(entry, "_startup_health", lambda: None)
    monkeypatch.setattr(entry, "_ensure_user_config", lambda: "")
    monkeypatch.setattr(entry, "_ensure_project_root", lambda: None)

    assert entry.main([]) == 0
    assert "--radar" in captured["argv"]
