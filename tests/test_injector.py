"""指令注入中枢测试（v2.0.0）。

覆盖：留言箱读写、stdin 直写、stdin 失败降级、重启注入、跨进程契约。
"""

from __future__ import annotations

import json
import os

from src.core.injector import AgentInjector, InboxMessage, InboxStore


# ---------------------------------------------------------------------------
# 留言箱
# ---------------------------------------------------------------------------
def test_append_and_read_back(tmp_path):
    """追加的消息要能原样读回来。"""
    inbox = InboxStore(str(tmp_path / "sentinel_inbox.json"))
    inbox.append(
        InboxMessage(
            session_id="a",
            verdict="STUCK",
            rule_id="context_stuck",
            prompt="先说说你的任务目标。",
        )
    )

    messages = inbox.messages()
    assert len(messages) == 1
    assert messages[0].verdict == "STUCK"
    assert messages[0].rule_id == "context_stuck"
    assert messages[0].prompt == "先说说你的任务目标。"
    assert messages[0].id


def test_pending_only_returns_undelivered(tmp_path):
    """已送达的消息不再算「待投」。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    message = inbox.append(InboxMessage(prompt="第一条"))
    inbox.append(InboxMessage(prompt="第二条"))

    assert len(inbox.pending()) == 2

    assert inbox.mark_delivered([message.id]) == 1
    pending = inbox.pending()
    assert len(pending) == 1
    assert pending[0].prompt == "第二条"


def test_inbox_file_is_human_readable_json(tmp_path):
    """留言箱必须是**人能直接打开看**的 JSON。"""
    path = tmp_path / "sentinel_inbox.json"
    inbox = InboxStore(str(path))
    inbox.append(InboxMessage(prompt="请先复述任务目标", verdict="STUCK"))

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["messages"]) == 1
    assert raw["messages"][0]["prompt"] == "请先复述任务目标"


def test_corrupt_inbox_is_treated_as_empty(tmp_path):
    """留言箱坏了不能把整个注入功能带崩。"""
    path = tmp_path / "inbox.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    inbox = InboxStore(str(path))

    assert inbox.messages() == []
    # 还能继续写。
    inbox.append(InboxMessage(prompt="还能写"))
    assert len(inbox.messages()) == 1


def test_render_for_prompt_includes_pending_messages(tmp_path):
    """重启注入：待投消息要出现在任务提示里。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    inbox.append(InboxMessage(prompt="第一件事：别再乱删文件了"))
    inbox.append(InboxMessage(prompt="第二件事：先跑测试"))

    text = inbox.render_for_prompt()
    assert "别再乱删文件了" in text
    assert "先跑测试" in text
    assert "哨兵留言" in text


def test_render_for_prompt_is_empty_when_nothing_pending(tmp_path):
    """没有待投消息时不该塞一段空话进任务提示。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    assert inbox.render_for_prompt() == ""


# ---------------------------------------------------------------------------
# 注入器
# ---------------------------------------------------------------------------
def test_inject_writes_to_stdin_when_available(tmp_path):
    """有 stdin 通道时，提示要真的写进去。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    written = []
    injector = AgentInjector(
        inbox=inbox,
        stdin_writer=lambda text: written.append(text) or True,
        agent_label="codex",
    )

    result = injector.inject("回到任务目标", session_id="s1", verdict="STUCK")

    assert result.ok is True
    assert result.channel == "stdin"
    assert written == ["回到任务目标"]
    # 送达也要留痕。
    assert len(inbox.messages()) == 1
    assert inbox.messages()[0].delivered is True


def test_inject_falls_back_to_inbox_when_stdin_fails(tmp_path):
    """stdin 写失败要降级到留言箱，而不是报错。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    injector = AgentInjector(
        inbox=inbox,
        stdin_writer=lambda text: False,
        agent_label="codex",
    )

    result = injector.inject("回到任务目标", verdict="STUCK")

    assert result.ok is True
    assert result.channel == "inbox"
    assert result.inbox_path.endswith("inbox.json")
    # 必须告诉用户接下来该干什么。
    assert "重新跑一次哨兵" in result.next_step
    # 消息留在箱里，等下次启动补投。
    assert len(inbox.pending()) == 1


def test_inject_falls_back_when_stdin_raises(tmp_path):
    """stdin 抛异常也只降级，绝不外泄。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))

    def boom(text):
        raise OSError("管道已关闭")

    injector = AgentInjector(inbox=inbox, stdin_writer=boom)
    result = injector.inject("回到任务目标")

    assert result.ok is True
    assert result.channel == "inbox"


def test_inject_without_stdin_goes_straight_to_inbox(tmp_path):
    """压根没有 stdin 通道（CLI 不支持）时直接走留言箱。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    injector = AgentInjector(inbox=inbox)

    result = injector.inject("回到任务目标")

    assert result.channel == "inbox"
    assert injector.capabilities()["stdin"] is False
    assert injector.capabilities()["inbox"] is True


def test_inject_empty_prompt_does_nothing(tmp_path):
    """空提示不该留下垃圾留言。"""
    inbox = InboxStore(str(tmp_path / "inbox.json"))
    injector = AgentInjector(inbox=inbox)

    result = injector.inject("   ")

    assert result.ok is False
    assert inbox.messages() == []


def test_inbox_path_is_inside_workdir(tmp_path):
    """留言箱必须落在被监控目录旁边，用户找得到。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    inbox = InboxStore(str(workdir / "sentinel_inbox.json"))

    assert os.path.dirname(inbox.path) == str(workdir)
