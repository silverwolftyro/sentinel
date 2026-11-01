"""v3.0.1 回归：雷达增强（工作区提取 · 扫描根目录 · 活跃过滤）。

背景
====

v3.0.0 的雷达在用户实测里暴露了三个问题：

1. **扫出来的全是「未知工作区」** —— 真实原因：新版 codex 的 rollout
   把 ``cwd`` 放在 ``payload.cwd`` 里，而旧代码只找顶层和 ``item``；
2. **只扫 ``~/.codex``** —— 用户自己的 mcode 会话在
   ``~/.minimax/v2/sessions``，根本没被覆盖；
3. **满屏历史垃圾** —— 一个 mcode 会话目录下有 4 个文件，
   再加上几十个早就跑完的会话，全列出来没法看。

本文件把这三件事都钉死，另外还盯住：

4. **工作区回退链** —— ``cwd`` → ``args``/``command`` → 进程 cwd →
   父目录名；最后一档**绝不**再显示「未知工作区」；
5. **会话 ID 与首条消息时间** —— 用户要能认出「这是我刚跑的那个任务」。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from src.radar import (
    DEFAULT_SCAN_ROOTS,
    agent_roots,
    describe_session,
    detect_agent,
    extract_cwd,
    extract_first_time,
    extract_session_id,
    extract_workspace,
    format_label,
    is_primary_log,
    resolve_scan_roots,
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
    lines = [json.dumps(item, ensure_ascii=False) for item in payloads]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


#: 新版 codex 的真实首行（v0.16x）—— cwd 在 ``payload`` 里。
CODEX_SESSION_META = {
    "timestamp": "2026-10-27T08:49:47.571Z",
    "ordinal": 0,
    "type": "session_meta",
    "payload": {
        "session_id": "01a17c8d-25f7-76a1-823e-dd93c1b8fdf5",
        "id": "01a17c8d-25f7-76a1-823e-dd93c1b8fdf5",
        "timestamp": "2026-10-27T08:49:47.512Z",
        "cwd": "C:\\Users\\alpha\\work\\project_alpha",
        "runtime_workspace_roots": ["C:\\Users\\alpha\\work\\project_alpha"],
        "originator": "codex_exec",
        "cli_version": "0.162.1",
    },
}


# ---------------------------------------------------------------------------
# 1. 扫描根目录（radar.scan_roots）
# ---------------------------------------------------------------------------
def test_默认清单含三个家目录():
    """任务书要求：默认含 ``.codex`` / ``.mcode`` / ``.claude``。"""
    for name in (".codex", ".mcode", ".claude"):
        assert name in DEFAULT_SCAN_ROOTS


def test_默认清单还含_minimax_会话目录():
    """MiniMax Code 真正落日志的地方也要在默认清单里。

    Windows 版 mcode 的家目录是 ``~/.minimax``，会话在
    ``~/.minimax/v2/sessions`` —— 不写进去，用户自己的会话一个都扫不到。
    """
    assert ".minimax/v2/sessions" in DEFAULT_SCAN_ROOTS


def test_相对路径按家目录解析(tmp_path, monkeypatch):
    """``.codex`` 解析成「家目录下的 .codex」。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)

    roots = resolve_scan_roots([".codex"], home=str(tmp_path))

    assert len(roots) == 1
    assert roots[0]["path"] == os.path.join(str(tmp_path), ".codex")
    assert roots[0]["agent"] == "codex"


def test_绝对路径原样使用(tmp_path, monkeypatch):
    """``D:\\项目`` 这种绝对路径原样使用。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)
    target = str(tmp_path / "项目")

    roots = resolve_scan_roots([target], home=str(tmp_path))

    assert roots[0]["path"] == target


def test_自定义路径不存在也照样列出(tmp_path, monkeypatch):
    """用户配的路径暂时不存在，也要列出来让人看得见。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)
    missing = str(tmp_path / "还没建")

    roots = resolve_scan_roots([missing], home=str(tmp_path))

    assert roots[0]["path"] == missing


def test_重复路径只留一条(tmp_path, monkeypatch):
    """同一条路径写两遍只算一个。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)

    roots = resolve_scan_roots(
        [".codex", ".codex"], home=str(tmp_path)
    )

    assert len(roots) == 1


def test_空清单不报错():
    """空清单 / 空串都跳过。"""
    assert resolve_scan_roots([]) == []
    assert resolve_scan_roots(["", "   "]) == []


def test_agent_roots_用配置的清单(tmp_path, monkeypatch):
    """``agent_roots(scan_roots=...)`` 走配置清单。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)

    roots = agent_roots(home=str(tmp_path), scan_roots=[".codex", ".claude"])

    paths = [item["path"] for item in roots]
    assert paths == [
        os.path.join(str(tmp_path), ".codex"),
        os.path.join(str(tmp_path), ".claude"),
    ]


def test_minimax路径认成_mcode(tmp_path, monkeypatch):
    """``.minimax`` 要被认成 mcode，不能是「未知」。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)

    roots = resolve_scan_roots(
        [".minimax/v2/sessions"], home=str(tmp_path)
    )

    assert roots[0]["agent"] == "mcode"
    assert roots[0]["label"] == "MCode"


def test_scan_roots不影响老调用():
    """不传 ``scan_roots`` 时，老行为一个字都没变。"""
    roots = agent_roots(home=os.path.expanduser("~"))
    assert isinstance(roots, list)


# ---------------------------------------------------------------------------
# 2. 工作区提取：payload 里的 cwd（真实 bug 的修复）
# ---------------------------------------------------------------------------
def test_cwd在_payload里也能提取():
    """新版 codex 把 cwd 放在 ``payload.cwd`` —— 必须能挖出来。"""
    line = json.dumps(CODEX_SESSION_META, ensure_ascii=False)

    assert extract_cwd("C:/tmp/x.jsonl", [line]) == (
        "C:\\Users\\alpha\\work\\project_alpha"
    )


def test_工作区根目录列表能顶替_cwd():
    """只有 ``runtime_workspace_roots`` 时也要能提取。"""
    line = json.dumps(
        {
            "type": "session_meta",
            "payload": {
                "runtime_workspace_roots": ["D:/work/roots"],
            },
        },
        ensure_ascii=False,
    )

    assert extract_cwd("C:/tmp/x.jsonl", [line]) == "D:/work/roots"


def test_新版协议能认出_codex():
    """``session_meta`` / ``response_item`` 都是 codex 的事件。"""
    assert detect_agent("C:/tmp/x.jsonl", [
        json.dumps(CODEX_SESSION_META),
    ]) == "codex"
    assert detect_agent("C:/tmp/x.jsonl", [
        json.dumps({"type": "response_item", "payload": {"type": "message"}}),
    ]) == "codex"


def test_新版响应条目算_codex():
    """``response_item`` 是 codex 新版 rollout 的消息条目。

    ⚠️ mcode 用的是 v0.6 的 ``item.*`` 协议（工具调用藏在
    ``item.toolCall`` 里），**不用** ``response_item`` ——
    所以这里不能为它编一个「像 mcode」的分支。
    """
    assert detect_agent("C:/tmp/x.jsonl", [
        json.dumps({"type": "response_item", "payload": {"type": "message"}}),
    ]) == "codex"


def test_老协议_item_toolCall算_mcode():
    """mcode v0.6：``item.toolCall`` 这个包装在就是 mcode。"""
    assert detect_agent("C:/tmp/x.jsonl", [
        json.dumps({
            "type": "item.completed",
            "item": {"type": "tool_call", "toolCall": {"name": "bash"}},
        }),
    ]) == "mcode"


# ---------------------------------------------------------------------------
# 3. 工作区回退链
# ---------------------------------------------------------------------------
def test_回退链第一步是日志字段():
    """有 cwd 字段就用它，来源标成 ``cwd``。"""
    line = json.dumps(CODEX_SESSION_META, ensure_ascii=False)

    workspace, source = extract_workspace("C:/tmp/x.jsonl", [line])

    assert workspace == "C:\\Users\\alpha\\work\\project_alpha"
    assert source == "cwd"


def test_回退链第二步是启动参数(tmp_path):
    """没有 cwd 字段时，从 ``args`` 里找**确实是目录**的路径。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    line = json.dumps(
        {"type": "turn.started", "args": ["codex", "--cwd", str(workdir)]},
        ensure_ascii=False,
    )

    workspace, source = extract_workspace("C:/tmp/x.jsonl", [line])

    assert workspace == str(workdir)
    assert source == "args"

def test_启动参数里的文件不算工作区(tmp_path):
    """参数里指向**文件**的路径不能当成工作区。"""
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    line = json.dumps(
        {"type": "turn.started", "args": ["codex", str(target)]},
        ensure_ascii=False,
    )

    _workspace, source = extract_workspace("C:/tmp/x.jsonl", [line])

    assert source == "file"


def test_回退链第三步是进程工作目录():
    """cwd 与 args 都没有时，用调用方查到的进程 cwd。"""

    def _resolver(path, agent):
        return "D:\\from\\process"

    workspace, source = extract_workspace(
        "C:/tmp/x.jsonl", [], _resolver, "codex"
    )

    assert workspace == "D:\\from\\process"
    assert source == "process"


def test_回退链最后一步是父目录名():
    """全都没有时，退到「父目录名 + 文件名」，**不是**「未知工作区」。"""
    path = os.path.join("C:\\Users\\x\\.codex\\sessions\\27", "rollout-a.jsonl")

    workspace, source = extract_workspace(path, [])

    assert workspace == "27" + os.sep + "rollout-a.jsonl"
    assert source == "file"


def test_进程反查只在前面都失败时才调用():
    """反查进程要枚举系统进程 —— 不能对每个文件都做一遍。"""
    calls = []

    def _resolver(path, agent):
        calls.append(path)
        return "D:\\from\\process"

    line = json.dumps(CODEX_SESSION_META, ensure_ascii=False)
    extract_workspace("C:/tmp/x.jsonl", [line], _resolver, "codex")

    assert calls == []


def test_进程反查抛异常也不炸():
    """反查失败只降级 —— 绝不能把雷达带崩。"""

    def _boom(path, agent):
        raise RuntimeError("模拟 psutil 炸了")

    workspace, source = extract_workspace("C:/tmp/x.jsonl", [], _boom, "codex")

    assert source == "file"
    assert workspace.endswith("x.jsonl")


def test_描述结果里不出现未知工作区(tmp_path):
    """端到端：提取不到工作区时，展示名里**没有**「未知工作区」。"""
    target = tmp_path / "27" / "rollout-a.jsonl"
    _write_jsonl(target, [{"type": "thinking", "thinking": "旧会话"}])

    session = describe_session(str(target))

    assert session is not None
    assert "未知工作区" not in session.label
    assert session.workspace.endswith("rollout-a.jsonl")
    assert session.workspace_source == "file"
    # 「父目录名 + 文件名」只是标识，**不能**写进 cwd。
    assert session.cwd == ""


def test_展示名有兜底参数():
    """``format_label`` 支持兜底标识。"""
    assert format_label("codex", "") == "[Codex] 未知工作区"
    assert format_label("codex", "", "27\\rollout-a.jsonl") == (
        "[Codex] 27\\rollout-a.jsonl"
    )


# ---------------------------------------------------------------------------
# 4. 会话 ID 与首条消息时间
# ---------------------------------------------------------------------------
def test_会话ID从内容里提取():
    """``payload.session_id`` 要能提取。"""
    line = json.dumps(CODEX_SESSION_META, ensure_ascii=False)

    assert extract_session_id("C:/tmp/x.jsonl", [line]) == (
        "01a17c8d-25f7-76a1-823e-dd93c1b8fdf5"
    )


def test_会话ID从文件名里提取():
    """内容里没有时，从 rollout 文件名末尾的 UUID 里提取。"""
    path = os.path.join(
        "C:\\Users\\x\\.codex\\sessions\\27",
        "rollout-2026-10-27T16-49-47-01a17c8d-25f7-76a1-823e-dd93c1b8fdf5.jsonl",
    )

    assert extract_session_id(path, []) == (
        "01a17c8d-25f7-76a1-823e-dd93c1b8fdf5"
    )


def test_会话ID从目录名里提取():
    """mcode 的会话目录名形如 ``…-session_<id>``。"""
    path = os.path.join(
        "C:\\Users\\x\\.minimax\\v2\\sessions\\2026\\10\\26",
        "14-31-44-748-session_abcDEF123",
        "messages.jsonl",
    )

    assert extract_session_id(path, []) == "abcDEF123"


def test_首条消息时间从ISO串提取():
    """``2026-10-27T08:49:47.571Z`` 要能解析成 epoch 秒。"""
    line = json.dumps(CODEX_SESSION_META, ensure_ascii=False)

    stamp = extract_first_time("C:/tmp/x.jsonl", [line])

    assert stamp > 0
    assert abs(stamp - 1793090987.571) < 1.0


def test_首条消息时间从毫秒提取():
    """mcode 的 ``message.timestamp`` 是**毫秒**。"""
    line = json.dumps(
        {"message": {"role": "user", "timestamp": 1793161417473}},
        ensure_ascii=False,
    )

    stamp = extract_first_time("C:/tmp/x.jsonl", [line])

    assert abs(stamp - 1793161417.473) < 1.0


def test_时间提取不到就是零():
    """没有时间戳就是 0（不是当前时间 —— 那样会骗人）。"""
    assert extract_first_time("C:/tmp/x.jsonl", [{"a": 1}]) == 0.0


# ---------------------------------------------------------------------------
# 5. 主日志 vs 附属文件
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "messages.jsonl",
        "history.jsonl",
        "rollout-2026-10-27T16-49-47-abc.jsonl",
    ],
)
def test_主日志判定为真(name):
    """会话主日志要能认出来。"""
    assert is_primary_log(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "llm-call.json",
        "history-catalog.json",
        "user-message-locators.jsonl",
        "state.sqlite",
    ],
)
def test_附属文件判定为假(name):
    """索引 / 目录 / 快照这类附属文件要被折叠。"""
    assert is_primary_log(name) is False


def test_claude的jsonl算主日志():
    """claude 的项目目录下的会话文件没有固定名字。"""
    assert is_primary_log("abc-123.jsonl", "claude") is True


# ---------------------------------------------------------------------------
# 6. 扫描入口
# ---------------------------------------------------------------------------
def test_扫描能用配置的根目录(tmp_path, monkeypatch):
    """``scan_agent_sessions(scan_roots=...)`` 要真的按配置扫。"""
    monkeypatch.delenv("SENTINEL_RADAR_ROOTS", raising=False)
    target = tmp_path / "home" / ".codex" / "sessions" / "rollout-a.jsonl"
    _write_jsonl(target, [CODEX_SESSION_META])

    found = scan_agent_sessions(
        home=str(tmp_path / "home"),
        scan_roots=[".codex"],
        limit=10,
    )

    assert len(found) == 1
    assert found[0].cwd == "C:\\Users\\alpha\\work\\project_alpha"
    assert found[0].session_id == "01a17c8d-25f7-76a1-823e-dd93c1b8fdf5"
    assert found[0].first_time > 0


def test_扫描会带上进程反查回调(tmp_path):
    """回调能生效 —— 那是回退链第三步。"""
    target = tmp_path / "x" / "rollout-a.jsonl"
    _write_jsonl(target, [{"type": "thinking"}])

    found = scan_agent_sessions(
        roots=[{"agent": "codex", "label": "Codex", "path": str(tmp_path)}],
        process_cwd_resolver=lambda path, agent: "D:\\from\\process",
        limit=5,
    )

    assert found
    assert found[0].cwd == "D:\\from\\process"
    assert found[0].workspace_source == "process"


def test_扫描结果带_primary标记(tmp_path):
    """附属文件会被标成 ``primary=False``。"""
    main = tmp_path / "27" / "rollout-a.jsonl"
    _write_jsonl(main, [{"type": "thinking"}])
    extra = tmp_path / "27" / "llm-call.json"
    _write_jsonl(extra, [{"type": "thinking"}])

    found = scan_agent_sessions(
        roots=[{"agent": "codex", "label": "Codex", "path": str(tmp_path)}],
        limit=10,
    )

    by_name = {os.path.basename(item.path): item for item in found}
    assert by_name["rollout-a.jsonl"].primary is True
    assert by_name["llm-call.json"].primary is False


def test_扫描不抛异常(tmp_path):
    """审查侧异常一律降级 —— 雷达绝不把启动带崩。"""
    found = scan_agent_sessions(
        roots=[{"agent": "codex", "label": "Codex", "path": str(tmp_path)}],
        limit=5,
    )
    assert isinstance(found, list)


def test_描述会话用当前时间算年龄(tmp_path):
    """``now`` 参数决定 age 的基准（老行为不变）。"""
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [{"type": "thread.started"}])

    session = describe_session(
        str(target), now=time.time() + 10 * 3600
    )

    assert session is not None
    assert session.age >= 10 * 3600
    assert session.active is False
