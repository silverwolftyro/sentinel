"""v2.3.0 回归：雷达（radar）—— 发现已在终端里跑着的 Agent 会话日志。

覆盖三大块：

1. **识别** —— 靠路径特征、靠内容特征认出 codex / mcode / claude；
2. **提取** —— 从日志里挖出工作目录、拼出展示名；
3. **扫描** —— 只读地把「正在被写」的日志挑出来，并且**绝不抛异常**。

最关键的一条是 codex 与 mcode 的区分：两者 v0.6 协议都用
``item.*`` 事件，**唯一**可靠的差别是 ``item.toolCall``
这个嵌套包装在不在 —— mcode 有、codex 没有。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from src.radar import (
    ACTIVE_AGE_SECONDS,
    AGENT_KEYS,
    AGENT_LABELS,
    DiscoveredSession,
    agent_roots,
    describe_session,
    detect_agent,
    extract_cwd,
    format_label,
    scan_agent_sessions,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _write_jsonl(path, payloads):
    """往文件里写若干行 JSON（模拟 Agent 的会话日志）。

    :param path: 目标文件。
    :param payloads: JSON 可序列化的对象列表。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(item, ensure_ascii=False) for item in payloads
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _codex_line(**extra):
    """造一行 codex 格式的日志。

    :param extra: 追加到顶层的字段。
    :return: JSON 字符串。
    """
    payload = {"type": "thread.started", "thread_id": "t-1"}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. detect_agent —— 路径特征
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path, expected",
    [
        (r"C:\Users\x\.codex\sessions\2026\rollout.jsonl", "codex"),
        (r"C:\Users\x\.mcode\sessions\x.jsonl", "mcode"),
        (r"C:\Users\x\.claude\projects\proj\session.jsonl", "claude"),
        ("/home/user/.codex/sessions/a.jsonl", "codex"),
        ("/home/user/.MCODE/sessions/a.jsonl", "mcode"),
    ],
)
def test_detect_agent_by_path(path, expected):
    """路径里带 Agent 家目录名就该认出来（大小写不敏感）。"""
    assert detect_agent(path, []) == expected


# ---------------------------------------------------------------------------
# 2. detect_agent —— 内容特征
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload, expected",
    [
        # codex：会话级事件
        ({"type": "thread.started", "thread_id": "x"}, "codex"),
        ({"type": "turn.completed", "usage": {}}, "codex"),
        # codex：item 里**没有** toolCall
        (
            {
                "type": "item.completed",
                "item": {"id": "i0", "type": "agent_message", "text": "hi"},
            },
            "codex",
        ),
        # mcode：item 里有 toolCall 包装
        (
            {
                "type": "item.completed",
                "item": {
                    "id": "i0",
                    "type": "tool_call",
                    "toolCall": {"name": "Bash"},
                },
            },
            "mcode",
        ),
        # mcode：旧协议 tool_call
        (
            {"type": "tool_call", "tool_call": {"name": "Bash"}},
            "mcode",
        ),
        # mcode：旧协议 thinking / text
        ({"type": "thinking", "thinking": "我在想"}, "mcode"),
        ({"type": "text", "text": "好的"}, "mcode"),
        # claude：消息容器
        (
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            },
            "claude",
        ),
        # claude：会话初始化
        (
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            "claude",
        ),
    ],
)
def test_detect_agent_by_content(payload, expected):
    """路径不带任何特征时，靠内容事件类型认 Agent。"""
    path = r"C:\tmp\some\session\log.jsonl"
    assert detect_agent(path, [json.dumps(payload)]) == expected


def test_detect_agent_garbage_is_unknown():
    """垃圾文本认不出来就是 unknown。"""
    assert detect_agent(r"C:\tmp\x\log.jsonl", ["这不是 JSON"]) == "unknown"
    assert detect_agent(r"C:\tmp\x\log.jsonl", []) == "unknown"


def test_detect_agent_path_wins_over_content():
    """路径特征优先于内容特征。"""
    path = r"C:\Users\x\.claude\projects\p\log.jsonl"
    line = json.dumps({"type": "thread.started", "thread_id": "x"})
    assert detect_agent(path, [line]) == "claude"


# ---------------------------------------------------------------------------
# 3. extract_cwd
# ---------------------------------------------------------------------------
def test_extract_cwd_from_top_level():
    """顶层 cwd 字段。"""
    line = json.dumps({"type": "thread.started", "cwd": "D:\\work\\p"})
    assert extract_cwd(r"C:\tmp\x.jsonl", [line]) == "D:\\work\\p"


def test_extract_cwd_from_item():
    """item 里的 cwd 字段。"""
    line = json.dumps({"type": "item.completed", "item": {"cwd": "D:/work/q"}})
    assert extract_cwd(r"C:\tmp\x.jsonl", [line]) == "D:/work/q"


def test_extract_cwd_missing_returns_empty():
    """没有 cwd 就是空字符串。"""
    line = json.dumps({"type": "thread.started", "thread_id": "x"})
    assert extract_cwd(r"C:\tmp\x.jsonl", [line]) == ""
    assert extract_cwd(r"C:\tmp\x.jsonl", []) == ""


def test_extract_cwd_rejects_url():
    """URL 不算路径。"""
    line = json.dumps(
        {"type": "thread.started", "cwd": "https://example.com/repo"}
    )
    assert extract_cwd(r"C:\tmp\x.jsonl", [line]) == ""


def test_extract_cwd_takes_first_candidate():
    """多个候选取第一个出现的。"""
    first = json.dumps({"type": "turn.started", "cwd": "D:\\work\\first"})
    second = json.dumps({"type": "turn.completed", "cwd": "D:\\work\\second"})
    assert extract_cwd(r"C:\tmp\x.jsonl", [first, second]) == "D:\\work\\first"


# ---------------------------------------------------------------------------
# 4. format_label
# ---------------------------------------------------------------------------
def test_format_label_with_cwd():
    """有工作目录时拼成 ``[Agent] 路径``。"""
    assert format_label("codex", "D:\\work\\p") == "[Codex] D:\\work\\p"
    assert format_label("mcode", "D:/work/q") == "[MCode] D:/work/q"
    assert format_label("claude", "/home/u/proj") == "[Claude] /home/u/proj"


def test_format_label_without_cwd():
    """没有工作目录时说「未知工作区」。"""
    assert format_label("codex", "") == "[Codex] 未知工作区"


def test_format_label_unknown_agent():
    """认不出来的 Agent 前缀用「未知」。"""
    assert format_label("gemini", "D:\\work\\p") == "[未知] D:\\work\\p"
    assert format_label("", "") == "[未知] 未知工作区"


# ---------------------------------------------------------------------------
# 5-6. describe_session
# ---------------------------------------------------------------------------
def test_describe_session_reads_details(tmp_path):
    """能认出 Agent、工作目录，并给出展示名。"""
    target = tmp_path / "a.jsonl"
    _write_jsonl(
        target,
        [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.completed", "cwd": "D:\\work\\p"},
        ],
    )

    session = describe_session(str(target))

    assert session is not None
    assert session.agent == "codex"
    assert session.agent_label == "Codex"
    assert session.cwd == "D:\\work\\p"
    assert session.label == "[Codex] D:\\work\\p"
    assert session.size > 0
    assert session.age >= 0
    assert session.active is True
    assert session.path == os.path.abspath(str(target))


def test_describe_session_missing_file(tmp_path):
    """文件不存在返回 None（不抛异常）。"""
    assert describe_session(str(tmp_path / "nope.jsonl")) is None


def test_describe_session_empty_file(tmp_path):
    """空文件返回 None。"""
    target = tmp_path / "empty.jsonl"
    target.write_text("", encoding="utf-8")
    assert describe_session(str(target)) is None


def test_describe_session_directory_returns_none(tmp_path):
    """目录不是普通文件，返回 None。"""
    assert describe_session(str(tmp_path)) is None


def test_describe_session_to_dict(tmp_path):
    """to_dict 字段原样输出，便于直接 JSON 化。

    v3.0.1 扩了五个字段：工作区 / 工作区来源 / 会话 ID / 首条时间 /
    是否主日志。v3.0.2 再加三个：扫描根优先级 / 是否压缩 /
    能不能接入实时审查。v3.5.2 再加一个：自动拔出的任务锚点。
    """
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [{"type": "thread.started"}])

    session = describe_session(str(target))
    assert session is not None

    data = session.to_dict()
    assert set(data) == {
        "path",
        "agent",
        "agent_label",
        "cwd",
        "label",
        "size",
        "mtime",
        "age",
        "active",
        "workspace",
        "workspace_source",
        "session_id",
        "first_time",
        "primary",
        "priority",
        "compressed",
        "attachable",
        "task_goal",
    }
    assert data["agent"] == "codex"
    # v3.0.1：提取不到工作目录时**不再**显示「未知工作区」，
    # 而是退到「父目录名 + 文件名」。
    assert "未知工作区" not in json.loads(json.dumps(data))["label"]


def test_describe_session_uses_now(tmp_path):
    """now 参数决定 age 的基准。"""
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [{"type": "thread.started"}])

    session = describe_session(
        str(target), now=time.time() + 10 * 3600
    )
    assert session is not None
    assert session.age >= 10 * 3600
    assert session.active is False


# ---------------------------------------------------------------------------
# 7-8. scan_agent_sessions
# ---------------------------------------------------------------------------
def test_scan_agent_sessions_active_and_stale(tmp_path):
    """新鲜的算活跃，很久没更新的算不活跃，并按 mtime 倒序。"""
    fresh = tmp_path / ".codex" / "sessions" / "rollout.jsonl"
    _write_jsonl(
        fresh,
        [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started", "cwd": "D:\\work\\p"},
        ],
    )

    stale = tmp_path / ".mcode" / "old.jsonl"
    _write_jsonl(stale, [{"type": "thinking", "thinking": "旧会话"}])
    old_time = time.time() - 10 * 3600
    os.utime(stale, (old_time, old_time))

    sessions = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path / ".codex"),
            },
            {
                "agent": "mcode",
                "label": "MCode",
                "path": str(tmp_path / ".mcode"),
            },
        ]
    )

    assert len(sessions) == 2
    assert sessions[0].agent == "codex"
    assert sessions[0].active is True
    assert sessions[0].cwd == "D:\\work\\p"
    assert sessions[1].agent == "mcode"
    assert sessions[1].active is False
    # mtime 倒序：新鲜的在前。
    assert sessions[0].mtime >= sessions[1].mtime


def test_scan_agent_sessions_uses_home(tmp_path):
    """不传 roots 时用家目录清单去找 Agent 家目录。"""
    target = tmp_path / ".codex" / "sessions" / "rollout.jsonl"
    _write_jsonl(target, [{"type": "thread.started"}])

    sessions = scan_agent_sessions(home=str(tmp_path))

    assert len(sessions) == 1
    assert sessions[0].agent == "codex"
    assert sessions[0].active is True


def test_scan_agent_sessions_finds_agent_dirs_under_root(tmp_path):
    """root 之下以点开头的 Agent 家目录不能被跳过。"""
    target = tmp_path / ".codex" / "sessions" / "rollout.jsonl"
    _write_jsonl(target, [{"type": "thread.started"}])

    sessions = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path),
            }
        ]
    )

    assert len(sessions) == 1
    assert sessions[0].agent == "codex"


def test_scan_agent_sessions_skips_other_hidden_dirs(tmp_path):
    """与 Agent 无关的隐藏目录要跳过。"""
    hidden = tmp_path / ".git" / "log.jsonl"
    _write_jsonl(hidden, [{"type": "thread.started"}])

    sessions = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path),
            }
        ]
    )

    assert sessions == []


def test_scan_agent_sessions_missing_root_is_safe(tmp_path):
    """root 目录不存在时不抛异常，返回空列表。"""
    assert (
        scan_agent_sessions(
            roots=[
                {
                    "agent": "codex",
                    "label": "Codex",
                    "path": str(tmp_path / "does-not-exist"),
                }
            ]
        )
        == []
    )


def test_scan_agent_sessions_respects_limit(tmp_path):
    """结果条数受 limit 约束。"""
    for index in range(3):
        _write_jsonl(
            tmp_path / ".codex" / "sessions" / f"r{index}.jsonl",
            [{"type": "thread.started"}],
        )

    sessions = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path / ".codex"),
            }
        ],
        limit=2,
    )

    assert len(sessions) == 2


def test_scan_agent_sessions_max_age_override(tmp_path):
    """max_age 决定活跃判定阈值。"""
    target = tmp_path / ".codex" / "sessions" / "rollout.jsonl"
    _write_jsonl(target, [{"type": "thread.started"}])
    old_time = time.time() - 10 * 3600
    os.utime(target, (old_time, old_time))

    sessions = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path / ".codex"),
            }
        ]
    )
    assert sessions[0].active is False

    sessions = scan_agent_sessions(
        roots=[
            {
                "agent": "codex",
                "label": "Codex",
                "path": str(tmp_path / ".codex"),
            }
        ],
        max_age=24 * 3600,
    )
    assert sessions[0].active is True


# ---------------------------------------------------------------------------
# 9-10. agent_roots 与常量
# ---------------------------------------------------------------------------
def test_agent_roots_finds_agent_dirs(tmp_path, monkeypatch):
    """家目录下的 Agent 目录会被列出来。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".claude").mkdir()

    roots = agent_roots(home=str(tmp_path))
    found = {item["agent"]: item for item in roots}

    assert found["codex"]["path"] == str(tmp_path / ".codex")
    assert found["codex"]["label"] == "Codex"
    assert found["claude"]["path"] == str(tmp_path / ".claude")
    # 不存在的家目录不返回。
    assert "mcode" not in found


def test_agent_roots_env_var_appends(tmp_path, monkeypatch):
    """环境变量里的额外目录会被追加（哪怕不存在）。"""
    extra = tmp_path / "extra_logs"
    monkeypatch.setenv("SENTINEL_RADAR_ROOTS", str(extra))

    roots = agent_roots(home=str(tmp_path))
    paths = [item["path"] for item in roots]

    assert str(extra) in paths
    extra_entry = next(
        item for item in roots if item["path"] == str(extra)
    )
    assert extra_entry["agent"] == "unknown"


def test_agent_roots_env_var_explicit_agent(tmp_path, monkeypatch):
    """``路径=agent`` 形态按写的来。"""
    extra = tmp_path / "claude_logs"
    monkeypatch.setenv("SENTINEL_RADAR_ROOTS", f"{extra}=claude")

    roots = agent_roots(home=str(tmp_path))
    extra_entry = next(
        item for item in roots if item["path"] == str(extra)
    )

    assert extra_entry["agent"] == "claude"
    assert extra_entry["label"] == "Claude"


def test_agent_roots_env_var_semicolon_separated(tmp_path, monkeypatch):
    """分号分隔的多个目录都会追加。"""
    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setenv(
        "SENTINEL_RADAR_ROOTS", f"{first};{second}=mcode"
    )

    roots = agent_roots(home=str(tmp_path))
    paths = [item["path"] for item in roots]

    assert str(first) in paths
    assert str(second) in paths
    second_entry = next(
        item for item in roots if item["path"] == str(second)
    )
    assert second_entry["agent"] == "mcode"


def test_agent_labels_cover_four_agents():
    """展示名表要有 codex / mcode / claude / dsh / unknown 五项。

    v3.0.2 加了 ``dsh`` —— DeepSeek harness（``~/.dsh/sessions``）
    也要有自己的展示名，不能混进「未知」。
    """
    assert set(AGENT_LABELS) == {
        "codex",
        "mcode",
        "claude",
        "dsh",
        "unknown",
    }
    assert AGENT_LABELS["codex"] == "Codex"
    assert AGENT_LABELS["mcode"] == "MCode"
    assert AGENT_LABELS["claude"] == "Claude"
    assert AGENT_LABELS["dsh"] == "DeepSeek"
    assert AGENT_LABELS["unknown"] == "未知"


def test_agent_keys_order():
    """AGENT_KEYS 的顺序即展示顺序（v3.0.2 加了 dsh）。"""
    assert AGENT_KEYS == ("codex", "mcode", "claude", "dsh")


def test_active_age_seconds_constant():
    """活跃阈值是 5 分钟。"""
    assert ACTIVE_AGE_SECONDS == 300.0


def test_discovered_session_dataclass_fields():
    """数据类字段齐全，且可以直接构造。"""
    session = DiscoveredSession(
        path="C:/x.log",
        agent="codex",
        agent_label="Codex",
        cwd="C:/w",
        label="[Codex] C:/w",
        size=10,
        mtime=1.0,
        age=0.0,
        active=True,
    )

    assert session.to_dict()["path"] == "C:/x.log"
    assert session.to_dict()["active"] is True
