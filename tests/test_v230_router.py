"""v2.3.0 回归：Web UI 路由化（独立设置页）。

背景
====

v2.2.x 之前哨兵只有**一个**页面，功能全堆在上面：
顶栏一堆按钮、弹窗、模态框……用户抱怨「界面元素堆砌过多」。

v2.3.0 引入**前端路由**：

* ``/`` —— 主看板；
* ``/settings`` —— **独立全屏**的设置页
  （不是抽屉、不是弹窗、不是模态框）。

本文件钉住：

1. ``/settings`` 有**自己的 URL**，直接访问能用（刷新不丢）；
2. 顶栏有【⚙ 设置】入口，且它是**带 data-nav 的站内链接**
   （前端路由器靠这个属性接管）；
3. 设置页要能通过 API **读**配置、**写回**配置，并触发热重载；
4. 密钥**打码**下发、**打码值不会被写回去**（不能把真密钥抹成 ****）；
5. 配置写回是**外科手术式的** —— 注释必须保留。
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from src.gui.server import create_app

CONFIG_SAMPLE = """# 哨兵配置（测试用）
llm:
  # 主力模型
  provider: "ollama"
  base_url: "http://localhost:11434"
  model: "qwen2.5:7b"
  timeout: 20
  # 这行注释在保存后**必须**还在
  risk_threshold: 0.7
  api_key: "sk-真的密钥"

rules:
  enabled: true
  # 高危命令正则（增量追加到内置表之后）
  dangerous_commands:
    - '\\brm\\s+-rf\\b'
    - '\\bsudo\\b'
  hot_reload:
    enabled: true
    interval: 1.0

alert:
  enabled: true
  channels:
    - type: "terminal"
      enabled: false
    - type: "webhook"
      enabled: true
      url: "https://example.invalid/hook"
      format: "generic"
      secret: "wh-真的密钥"
"""


def _make_app(tmp_path, **kwargs):
    """造一个带配置文件的看板应用。"""
    watch = tmp_path / "watch.jsonl"
    watch.write_text("", encoding="utf-8")
    alert = tmp_path / "alert.log"
    alert.write_text("", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_SAMPLE, encoding="utf-8")
    kwargs.setdefault("config_path", str(config))
    return create_app(str(watch), str(alert), start_watcher=False, **kwargs)


# ---------------------------------------------------------------------------
# 路由：独立设置页有自己的 URL
# ---------------------------------------------------------------------------
def test_设置页有自己的地址(tmp_path):
    """``/settings`` 必须是一个**能直接访问**的地址。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/settings")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_设置页是独立全屏界面(tmp_path):
    """设置页不能是看板里的一个抽屉/弹窗 —— 它得是**另一个页面**。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/settings").text
    # 它有自己的标题、自己的返回链接、自己的分区。
    assert "设置" in html
    assert 'data-nav="/"' in html
    assert "模型路由" in html
    assert "通知通道" in html
    assert "挂载管理" in html


def test_看板有设置入口(tmp_path):
    """顶栏必须有【⚙ 设置】入口。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text
    assert 'href="/settings"' in html
    assert 'data-nav="/settings"' in html


def test_设置入口带data_nav(tmp_path):
    """前端路由器靠 ``data-nav`` 接管站内跳转。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text
    assert "settings-link" in html


def test_路由器脚本被引入(tmp_path):
    """前端路由脚本要在页面里。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text
    assert "/static/router.js" in html


def test_路由器脚本能下载(tmp_path):
    """路由器脚本本身得能取到（否则页面直接坏）。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/static/router.js")
    assert response.status_code == 200
    body = response.text
    assert "pushState" in body
    assert "popstate" in body
    assert "data-nav" in body


def test_看板脚本暴露路由契约(tmp_path):
    """页面必须暴露 ``init`` / ``destroy`` —— 不然路由切走会漏后台线程。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/static/dashboard.js").text
    assert "SentinelPage" in body
    assert "destroy" in body


def test_设置页脚本暴露路由契约(tmp_path):
    """设置页也要遵守同一套契约（v3.0.2 拆成了 6 个脚本）。

    ``settings.js`` 在 v3.0.2 被拆成「公共 + 5 个子页面」，
    每个子页面脚本都得暴露 ``SentinelPage`` 契约。
    """
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        for name in (
            "settings_common.js",
            "settings_index.js",
            "settings_llm.js",
            "settings_webhook.js",
            "settings_rules.js",
            "settings_sessions.js",
        ):
            response = client.get("/static/" + name)
            assert response.status_code == 200, name
            body = response.text
            assert "SentinelPage" in body, name
            assert "init" in body, name
            assert "destroy" in body, name


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def test_读配置接口(tmp_path):
    """``GET /api/config`` 要把当前配置给出来。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/config").json()
    assert payload["ok"] is True
    assert payload["llm"]["model"] == "qwen2.5:7b"
    assert len(payload["channels"]) == 2
    assert payload["sessions"]


def test_密钥会被打码(tmp_path):
    """密钥不能明文发到浏览器。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/config").json()
    assert payload["llm"]["api_key"] != "sk-真的密钥"
    assert payload["llm"]["api_key"].count("*") >= 4
    for channel in payload["channels"]:
        if channel.get("type") == "webhook":
            assert channel.get("secret") != "wh-真的密钥"


def test_写配置会落盘(tmp_path):
    """``POST /api/config`` 要真的写回文件。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/config",
            json={"llm": {"model": "qwen2.5:14b"}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "llm.model" in body["applied"]

    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "qwen2.5:14b" in text


def test_写配置保留注释(tmp_path):
    """⚠️ 核心约束：写回**不能**抹掉注释。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/config", json={"llm": {"model": "qwen2.5:14b"}})
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "这行注释在保存后**必须**还在" in text
    assert "高危命令正则（增量追加到内置表之后）" in text
    assert "# 哨兵配置（测试用）" in text


def test_写配置会留备份(tmp_path):
    """改配置前先备份 —— 用户改错了还能回来。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        body = client.post(
            "/api/config", json={"llm": {"model": "qwen2.5:14b"}}
        ).json()
    backup = body.get("backup")
    assert backup
    assert os.path.isfile(backup)
    assert "qwen2.5:7b" in open(backup, encoding="utf-8").read()


def test_打码值不会被写回去(tmp_path):
    """⚠️ 用户没改密钥时，打码值**绝不能**覆盖真密钥。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/api/config",
            json={"llm": {"api_key": "********"}},
        )
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "sk-真的密钥" in text
    assert "********" not in text


def test_写规则列表(tmp_path):
    """L1 规则列表要能整体替换。

    ⚠️ 用 **YAML 解析后的语义值**来断言，而不是断言文件里的字面量 ——
    正则里的反斜杠在 YAML 里有单引号 / 双引号两种写法，
    断言字面量会把「写法差异」误判成「写坏了」。
    """
    import yaml

    app = _make_app(tmp_path)
    with TestClient(app) as client:
        body = client.post(
            "/api/config",
            json={
                "rules": {
                    "dangerous_commands": ["\\bcurl\\b", "\\bshutdown\\b"]
                }
            },
        ).json()
    assert body["ok"] is True
    assert "rules.dangerous_commands" in body["applied"]

    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert parsed["rules"]["dangerous_commands"] == [
        "\\bcurl\\b",
        "\\bshutdown\\b",
    ]
    # 注释还在。
    assert "高危命令正则（增量追加到内置表之后）" in text


def test_找不到的键会被如实报出来(tmp_path):
    """⚠️ 没找到的键不能悄悄吞掉 —— 要报出来。

    这里用的是一个**在白名单里、但配置文件里没有**的键
    （示例配置没有 ``llm.routing`` 段）。
    """
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        body = client.post(
            "/api/config",
            json={"routing": {"enabled": True}},
        ).json()
    assert body["ok"] is True
    assert body["skipped"]
    assert "llm.routing.enabled" in body["skipped"]


def test_空改动不写文件(tmp_path):
    """什么都没改就别动文件（也别生成备份）。"""
    app = _make_app(tmp_path)
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    with TestClient(app) as client:
        body = client.post("/api/config", json={}).json()
    assert body["ok"] is True
    assert body["changed"] is False
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before
    assert not os.path.exists(str(tmp_path / "config.yaml.bak"))


def test_保存会触发热重载(tmp_path):
    """「保存并应用」要把规则**立刻**换掉。"""
    calls = []

    def reload_callback():
        calls.append(1)
        return {"ok": True, "message": "已换 12 条规则。"}

    app = _make_app(tmp_path, reload_callback=reload_callback)
    with TestClient(app) as client:
        body = client.post(
            "/api/config", json={"llm": {"model": "qwen2.5:14b"}}
        ).json()
    assert body["reloaded"] is True
    assert calls == [1]


def test_热重载失败不影响保存(tmp_path):
    """热重载炸了，**配置也已经存好了** —— 要说清楚，不能装作没存。"""

    def reload_callback():
        raise RuntimeError("规则编译失败")

    app = _make_app(tmp_path, reload_callback=reload_callback)
    with TestClient(app) as client:
        body = client.post(
            "/api/config", json={"llm": {"model": "qwen2.5:14b"}}
        ).json()
    assert body["ok"] is True
    assert body["reloaded"] is False
    assert "热重载失败" in body.get("reload_note", "")
    assert "qwen2.5:14b" in (tmp_path / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_坏JSON不会崩(tmp_path):
    """坏请求体只降级，不能把服务带崩。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/config",
            content=b"{ not json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True


# ---------------------------------------------------------------------------
# 挂载管理（设置页的第四块）
# ---------------------------------------------------------------------------
def test_配置接口带挂载列表(tmp_path):
    """设置页的「挂载管理」要能列出当前在监控的会话。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/config").json()
    sessions = payload["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["watch_path"]
    assert "id" in sessions[0]
