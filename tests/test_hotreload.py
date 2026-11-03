"""规则热重载测试（v2.2.0）。

覆盖：文件变更能被察觉、规则能**真的换掉**、坏配置**保留旧规则**
（不能让防线一次手滑就清零）、终端提示是用户要求的那句原文，
以及多路会话共用一份配置时只打印**一条**提示。
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from src.core.hotreload import RELOAD_MESSAGE, ConfigWatcher, RuleHotReloader
from src.rules.basic import RuleEngine


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write_config(path, patterns, *, replace=True) -> None:
    """写一份只含高危命令正则的配置。"""
    lines = [
        "rules:",
        "  enabled: true",
        "  dangerous_commands_replace: {}".format(
            "true" if replace else "false"
        ),
        "  dangerous_commands:",
    ]
    lines.extend("    - '{}'".format(item) for item in patterns)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tool_event(command: str):
    """造一个「Agent 要执行这条命令」的事件。"""
    return {
        "type": "tool_call",
        "tool": {"name": "Bash", "command": command, "arguments": {}},
        "source": "codex",
    }


class _FakeEngine:
    """假引擎 —— 只提供一个可替换的 ``rule_engine`` 属性。"""

    def __init__(self, rule_engine) -> None:
        self.rule_engine = rule_engine
        self.notifier = "原来的通知器"
        self.guardian = "原来的守护者"


# ---------------------------------------------------------------------------
# ConfigWatcher
# ---------------------------------------------------------------------------
def test_文件变了会回调(tmp_path):
    """配置一改，监听器就得知道。"""
    target = tmp_path / "config.yaml"
    target.write_text("rules: {}\n", encoding="utf-8")
    seen = []
    watcher = ConfigWatcher(str(target), seen.append)
    assert watcher.poll_once() is False

    target.write_text("rules: {enabled: true}\n", encoding="utf-8")
    assert watcher.poll_once() is True
    assert seen and seen[0].endswith("config.yaml")


def test_文件没变不回调(tmp_path):
    """没改就别打扰 —— 免得白跑一遍重载。"""
    target = tmp_path / "config.yaml"
    target.write_text("rules: {}\n", encoding="utf-8")
    calls = []
    watcher = ConfigWatcher(str(target), calls.append)
    for _ in range(3):
        watcher.poll_once()
    assert calls == []


def test_文件被删不回调(tmp_path):
    """文件被删（可能正在重命名）时不要触发回调。"""
    target = tmp_path / "config.yaml"
    target.write_text("rules: {}\n", encoding="utf-8")
    calls = []
    watcher = ConfigWatcher(str(target), calls.append)
    os.remove(str(target))
    assert watcher.poll_once() is False
    assert calls == []


def test_回调抛异常不影响监听(tmp_path):
    """回调炸了 —— 监听线程不能跟着死。"""
    target = tmp_path / "config.yaml"
    target.write_text("rules: {}\n", encoding="utf-8")

    def _boom(path):
        raise RuntimeError("炸了")

    watcher = ConfigWatcher(str(target), _boom)
    target.write_text("rules: {enabled: true}\n", encoding="utf-8")
    assert watcher.poll_once() is True
    # 还能继续用。
    target.write_text("rules: {enabled: false}\n", encoding="utf-8")
    assert watcher.poll_once() is True


def test_后台线程能发现变更(tmp_path):
    """真正跑起来的后台线程也要能发现变更。"""
    target = tmp_path / "config.yaml"
    target.write_text("rules: {}\n", encoding="utf-8")
    seen = []
    watcher = ConfigWatcher(
        str(target), seen.append, interval=0.05, settle=0.0
    )
    watcher.start()
    try:
        target.write_text("rules: {enabled: true}\n", encoding="utf-8")
        deadline = 5.0
        waited = 0.0
        while waited < deadline and not seen:
            waited += 0.05
            threading.Event().wait(0.05)
    finally:
        watcher.stop()
    assert seen, "后台线程没有发现配置变更"


# ---------------------------------------------------------------------------
# RuleHotReloader
# ---------------------------------------------------------------------------
def test_重载会换掉规则引擎(tmp_path):
    """热重载的**目的**就是换掉规则引擎。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"旧命令"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)
    before = engine.rule_engine
    result = reloader.reload_now()
    assert result["ok"] is True
    assert engine.rule_engine is not before


def test_新规则真的生效(tmp_path):
    """改完配置，新命令要能被拦下 —— 这才是「热重载」的意义。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"新命令甲"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)

    _write_config(config, [r"新命令乙"])
    reloader.reload_now()

    verdict = engine.rule_engine.check_event(_tool_event("执行 新命令乙 一下"))
    assert verdict["verdict"] == "DANGEROUS"
    assert verdict["rule_id"] == "dangerous_command"

    # 旧的那条已经不该再命中（配置里被替换掉了）。
    old = engine.rule_engine.check_event(_tool_event("执行 新命令甲 一下"))
    assert old["verdict"] == "ON_TRACK"


def test_坏配置保留旧规则(tmp_path):
    """YAML 写坏了 —— **保留旧规则**，不能把防线清零。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"要保留的命令"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)
    reloader.reload_now()
    good = engine.rule_engine

    config.write_text("rules: [这不是合法的 YAML\n", encoding="utf-8")
    result = reloader.reload_now()
    assert result["ok"] is False
    assert engine.rule_engine is good
    verdict = engine.rule_engine.check_event(_tool_event("执行 要保留的命令 一下"))
    assert verdict["verdict"] == "DANGEROUS"


def test_空配置保留旧规则(tmp_path):
    """空文件也算坏配置 —— 一样保留旧规则。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"要保留的命令"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)
    reloader.reload_now()
    good = engine.rule_engine

    config.write_text("", encoding="utf-8")
    result = reloader.reload_now()
    assert result["ok"] is False
    assert engine.rule_engine is good


def test_配置文件不见了保留旧规则(tmp_path):
    """文件被删 —— 保留旧规则并报错。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"要保留的命令"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)
    reloader.reload_now()
    good = engine.rule_engine

    os.remove(str(config))
    result = reloader.reload_now()
    assert result["ok"] is False
    assert engine.rule_engine is good


def test_提示语是用户要求的那句原文(tmp_path):
    """终端提示必须一字不差。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    messages = []
    reloader = RuleHotReloader(
        config_path=str(config),
        engine=engine,
        notify=messages.append,
    )
    reloader.reload_now()
    assert RELOAD_MESSAGE == "[SENTINEL] 检测到规则更新，热重载完成。"
    assert messages == [RELOAD_MESSAGE]


def test_多路会话共用一份配置只提示一次(tmp_path):
    """N 路会话不该打印 N 条提示。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engines = [_FakeEngine(RuleEngine({"rules": {}})) for _ in range(3)]
    messages = []
    reloader = RuleHotReloader(
        config_path=str(config),
        engines=engines,
        notify=messages.append,
    )
    result = reloader.reload_now()
    assert result["ok"] is True
    assert result["sessions"] == 3
    assert len(messages) == 1
    # 三路的规则都被换掉了。
    for engine in engines:
        assert engine.rule_engine is not None
        assert engine.rule_engine is engines[0].rule_engine


def test_热重载不重建通知器与守护者(tmp_path):
    """只换规则引擎 —— 通知通道与守护者是**服务**，不能被顺手重建。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)
    reloader.reload_now()
    assert engine.notifier == "原来的通知器"
    assert engine.guardian == "原来的守护者"


def test_热重载会写事件到时间线(tmp_path):
    """热重载本身也是一件「发生过的事」—— 该进时间线。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    events = []
    reloader = RuleHotReloader(
        config_path=str(config),
        engine=engine,
        event_sink=events.append,
    )
    reloader.reload_now()
    assert len(events) == 1
    assert events[0]["kind"] == "hot_reload"
    assert events[0]["timestamp"] > 0


def test_事件落地失败不影响重载(tmp_path):
    """事件写失败 —— 重载照样算成功。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))

    def _boom(payload):
        raise RuntimeError("炸了")

    reloader = RuleHotReloader(
        config_path=str(config),
        engine=engine,
        event_sink=_boom,
    )
    result = reloader.reload_now()
    assert result["ok"] is True


def test_规则条数被报出来(tmp_path):
    """重载结果要能说出「生效了几条规则」。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"命令一", r"命令二"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(config_path=str(config), engine=engine)
    result = reloader.reload_now()
    assert result["rules"] >= 2


@pytest.mark.parametrize("interval", [0.05, 1.0])
def test_轮询间隔不影响结果(tmp_path, interval):
    """间隔只是延迟，不影响正确性。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    reloader = RuleHotReloader(
        config_path=str(config), engine=engine, interval=interval
    )
    assert reloader.reload_now()["ok"] is True


def test_一次保存只回调一次(tmp_path):
    """一次「保存」在文件系统上可能是**两次**变化（先清空再写）。

    用户改一次配置就该看到**一条**提示 —— 不能吵他两遍。
    """
    target = tmp_path / "config.yaml"
    target.write_text("rules: {}\n", encoding="utf-8")
    calls = []
    watcher = ConfigWatcher(
        str(target), calls.append, settle=0.0
    )
    # 模拟编辑器的「先清空、再写入」。
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("rules: {enabled: true}\n")
    for _ in range(5):
        watcher.poll_once()
    assert len(calls) == 1


def test_坏配置只报一次(tmp_path):
    """坏配置不该把同一条报错喊两遍。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    engine = _FakeEngine(RuleEngine({"rules": {}}))
    messages = []
    reloader = RuleHotReloader(
        config_path=str(config),
        engine=engine,
        notify=messages.append,
    )
    config.write_text("rules: [这不是合法的 YAML\n", encoding="utf-8")
    reloader.reload_now()
    assert len(messages) == 1
    assert "已保留原规则" in messages[0]
    # ⚠️ 报错是**一行** —— YAML 的原生报错天生多行。
    assert "\n" not in messages[0]


def test_停止后不再监听(tmp_path):
    """停了就不该再触发 —— 收尾要干净。"""
    config = tmp_path / "config.yaml"
    _write_config(config, [r"任意"])
    seen = []
    watcher = ConfigWatcher(
        str(config), seen.append, interval=0.05, settle=0.0
    )
    watcher.start()
    watcher.stop()
    time.sleep(0.1)
    config.write_text("rules: {enabled: false}\n", encoding="utf-8")
    time.sleep(0.3)
    assert seen == []
