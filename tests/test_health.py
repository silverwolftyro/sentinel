"""开箱安全体检测试（v2.2.0）。

覆盖：四项检查各自的判定、Webhook 未配置时**必须**给出的那句警告、
终端 / Markdown 两种渲染、以及最要紧的一条 ——
**体检永远不该把哨兵启动搞崩**。
"""

from __future__ import annotations

import os

import pytest

from src.core.health import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    HealthItem,
    _short_error,
    check_cloud_api,
    check_ignore_rules,
    check_ollama,
    check_webhook,
    render_markdown,
    render_terminal,
    run_health_check,
    safe_print,
    summarize,
)

#: 用户明确要求的那句警告 —— 一个字都不能改。
WARNING_TEXT = "如果半夜跑挂，您将收不到手机推送！"


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def test_未配_webhook_必须警告而不是失败():
    """没配 Webhook 是**警告** —— 哨兵照样能干活。

    但那句「半夜收不到手机推送」必须一字不差地出现。
    """
    item = check_webhook({})
    assert item.status == STATUS_WARN
    assert WARNING_TEXT in item.hint
    assert item.hint.strip().startswith("⚠️")


def test_配了_webhook_报正常():
    """配了真实 Webhook 就报正常，并列出地址。"""
    config = {
        "alert": {
            "channels": [
                {
                    "type": "webhook",
                    "enabled": True,
                    "url": "https://example.invalid/hook",
                },
            ]
        }
    }
    item = check_webhook(config)
    assert item.status == STATUS_OK
    assert "https://example.invalid/hook" in item.detail


def test_没启用或没填地址的_webhook_不算配置():
    """示例配置里那一段是 ``enabled: false`` + 空地址 —— 不算配好。"""
    config = {
        "alert": {
            "channels": [
                {"type": "webhook", "enabled": False, "url": "https://x.invalid/h"},
                {"type": "webhook", "enabled": True, "url": ""},
                {"type": "console", "enabled": True},
            ]
        }
    }
    item = check_webhook(config)
    assert item.status == STATUS_WARN
    assert WARNING_TEXT in item.hint


# ---------------------------------------------------------------------------
# 本地模型 / 云端密钥
# ---------------------------------------------------------------------------
def test_主力不是本地模型_则跳过():
    """用户用云端模型时，别拿「Ollama 没开」吓唬他。"""
    item = check_ollama({"llm": {"provider": "deepseek"}})
    assert item.status == STATUS_SKIP
    assert "deepseek" in item.detail


def test_模型层关闭_则跳过():
    """``llm.enabled=false`` 时不该去戳模型。"""
    item = check_ollama({"llm": {"provider": "ollama", "enabled": False}})
    assert item.status == STATUS_SKIP


def test_主力不是云端_则跳过():
    """本地模型用户不该被「没配云端密钥」烦。"""
    item = check_cloud_api({"llm": {"provider": "ollama"}})
    assert item.status == STATUS_SKIP


def test_云端缺地址_直接说缺什么():
    """配置不全要说清楚缺哪一项，别只说「失败了」。"""
    item = check_cloud_api(
        {"llm": {"provider": "openai_compatible", "base_url": ""}}
    )
    assert item.status == STATUS_FAIL
    assert "base_url" in item.detail
    assert item.hint


# ---------------------------------------------------------------------------
# 快照排除规则
# ---------------------------------------------------------------------------
def test_有_gitignore_报正常(tmp_path):
    """.gitignore 存在 → 正常。"""
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    item = check_ignore_rules({}, str(tmp_path))
    assert item.status == STATUS_OK
    assert ".gitignore" in item.detail


def test_没有_gitignore_给警告(tmp_path):
    """没有排除规则时给**警告**，并说明内置清单兜底。"""
    item = check_ignore_rules({}, str(tmp_path))
    assert item.status == STATUS_WARN
    assert item.hint


def test_配置了排除目录_报正常(tmp_path):
    """配置里自定义了排除目录 → 正常。"""
    config = {"safe_point": {"excluded_dirs": ["node_modules"]}}
    item = check_ignore_rules(config, str(tmp_path))
    assert item.status == STATUS_OK
    assert item.extra.get("source") == "config"


def test_工作目录不存在_跳过(tmp_path):
    """目录都没了，跳过而不是报错。"""
    item = check_ignore_rules({}, str(tmp_path / "nope"))
    assert item.status == STATUS_SKIP


# ---------------------------------------------------------------------------
# 汇总与渲染
# ---------------------------------------------------------------------------
def test_汇总计数正确():
    """计数与判定必须对得上。"""
    items = [
        HealthItem(name="A", status=STATUS_OK),
        HealthItem(name="B", status=STATUS_WARN),
        HealthItem(name="C", status=STATUS_FAIL),
        HealthItem(name="D", status=STATUS_SKIP),
    ]
    info = summarize(items)
    assert info["counts"][STATUS_OK] == 1
    assert info["counts"][STATUS_WARN] == 1
    assert info["counts"][STATUS_FAIL] == 1
    assert info["counts"][STATUS_SKIP] == 1
    assert "1" in info["verdict"]


def test_终端报告包含那句警告():
    """终端报告里必须有那句警告 —— 这是体检存在的意义。"""
    items = [check_webhook({})]
    text = render_terminal(items)
    assert "哨兵安全体检报告" in text
    assert WARNING_TEXT in text


def test_终端报告_多行提示缩进整齐():
    """多行提示的缩进要齐 —— 歪成锯齿状就像坏了。"""
    items = [check_webhook({})]
    lines = render_terminal(items).splitlines()
    hint_lines = [
        line for line in lines if "半夜跑挂" in line or "alert.channels" in line
    ]
    assert len(hint_lines) >= 2
    indents = {len(line) - len(line.lstrip()) for line in hint_lines}
    assert len(indents) == 1


def test_markdown_报告含表格():
    """GUI 面板用 Markdown 渲染 —— 表格得在。"""
    items = [check_webhook({}), check_ignore_rules({}, os.getcwd())]
    text = render_markdown(items)
    assert "| 项目 | 状态 | 说明 |" in text
    assert WARNING_TEXT in text


# ---------------------------------------------------------------------------
# 绝不抛异常
# ---------------------------------------------------------------------------
def test_体检永远不抛异常():
    """坏配置也不能让体检崩 —— 崩了就等于没有体检。"""
    config = {"llm": {"provider": 123, "timeout": "不是数字"}}
    items = run_health_check(config, workdir="")
    assert len(items) == 4
    assert all(item.status for item in items)


def test_快速模式跳过联网两项():
    """``skip_network`` 时不該发任何网络请求。"""
    items = run_health_check({}, workdir="", skip_network=True)
    assert len(items) == 4
    assert items[0].status == STATUS_SKIP
    assert items[1].status == STATUS_SKIP


def test_单项检查异常时降级成跳过(monkeypatch):
    """某一项检查炸了，其它项照跑 —— 降级而不是整体失败。"""
    from src.core import health

    def _boom(*args, **kwargs):
        raise RuntimeError("炸了")

    monkeypatch.setattr(health, "check_ollama", _boom)
    items = health.run_health_check({}, workdir="", skip_network=False)
    assert len(items) == 4
    assert any("检查异常" in item.detail for item in items)


def _long_connection_error():
    """造一个「长得离谱」的连接异常。

    requests 的 ``ConnectionError`` 会把整个 urllib 追踪都塞进消息里 ——
    体检报告是给用户看的，不该出现那一百多字。
    """
    try:
        import requests

        return requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='localhost', port=11434): "
            "Max retries exceeded with url: /api/chat "
            "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection "
            "object at 0x0000021F0C3E1D90>: Failed to establish a new "
            "connection: [WinError 10061]'))"
        )
    except ImportError:  # pragma: no cover —— requests 是硬依赖
        return RuntimeError("boom " * 60)


def test_异常文案被压短():
    """超长异常必须被压成一句人话。"""
    text = _short_error(_long_connection_error())
    assert len(text) <= 140
    assert "连不上" in text


def test_超时异常单独识别():
    """超时要说「超时」，不能笼统说「连不上」。"""
    text = _short_error(TimeoutError("timed out"))
    assert "超时" in text


def test_safe_print_不因编码炸掉():
    """控制台代码页装不下字符时 —— 降级打印，不抛异常。"""
    safe_print("正常中文报告")
    safe_print("⚠️ 带 emoji 的报告")


@pytest.mark.parametrize("value", [None, "", 0, [], {}])
def test_体检项的默认值不炸(value):
    """HealthItem 的默认值必须站得住。"""
    item = HealthItem(name="X", detail=str(value))
    assert item.to_dict()["name"] == "X"
