"""日志高信噪比压缩引擎测试。

覆盖四条铁律：

1. ``tool_result`` 的正文（几百行源码、几 KB 编译日志）被丢掉，只留元数据；
2. 连续同类动作（连续 5 次 Read）被合并成一行；
3. 超长 ``thinking`` 只留首句 + 含意图动词的关键句；
4. 输出流首行永远是 ``[Task Goal]:``，末行永远是 ``[Current Action]:``。

以及压缩率验收：100 步、约 200KB 的原始日志 → 输出小于 10KB。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.core.log_compressor import (
    CompressionOptions,
    compress_events,
    compress_file,
    compress_jsonl,
    extract_intent,
)

#: 验收阈值 —— 压缩后必须小于 10KB。
MAX_OUTPUT_BYTES = 10 * 1024


# ---------------------------------------------------------------------------
# 模拟日志
# ---------------------------------------------------------------------------
#: 读取类的文件池。
_READ_FILES = [
    "src/core/auth.py",
    "src/core/user.py",
    "src/core/session.py",
    "src/core/token.py",
    "src/core/config.py",
    "src/core/guard.py",
    "src/core/audit.py",
    "src/core/policy.py",
    "src/core/router.py",
    "src/core/store.py",
]

#: 编辑类的文件池。
_EDIT_FILES = [
    "src/core/auth.py",
    "src/core/token.py",
    "src/core/session.py",
    "src/core/middleware.py",
    "src/core/schema.py",
]

#: 执行类的命令池。
_EXEC_COMMANDS = [
    "pytest tests/test_auth.py -q",
    "ruff check src/core",
    "python -m compileall -q src",
    "python scripts/migrate_users.py --dry-run",
    "pytest tests/ -k token -x",
]

#: 检索类的模式池。
_PATTERN_VALUES = [
    "def refresh_token",
    "class AuthMiddleware",
    "TODO\\(rotate",
    "raise HTTPException",
    "def verify_signature",
]

#: 思考模板 —— 每条都故意塞满语气词、寒暄与反复确认。
_THINKING_TEMPLATES = [
    "嗯，让我先看看这个文件到底是怎么实现的。\n"
    "我想我需要确认一下现有的实现是不是真的有问题。\n"
    "好的，接下来我要修改 {target} 里的过期判断逻辑，把令牌轮换补上。\n"
    "让我再确认一下刚才那处改动是不是生效了。",
    "好的，那我继续。\n"
    "我想我应该先看看相关测试覆盖到了哪些分支。\n"
    "接下来我要给 {target} 补一组回归测试，确保改动不会破坏旧行为。\n"
    "嗯，然后我再跑一遍测试看看结果。",
    "让我确认一下当前的状态。\n"
    "我需要重构 {target} 里的会话创建流程，把重复的代码抽出来。\n"
    "接下来我要实现一个统一的工厂函数，让创建逻辑只留一份。",
    "好，接下来我要修复 {target} 里的并发问题。\n"
    "让我再确认一下是不是真的能复现。\n"
    "好的，然后我要加一条日志，把这段逻辑的耗时记下来，方便以后排查。",
    "我想我需要先看清楚 {target} 的调用方都有谁。\n"
    "好的，接下来我要更新所有受影响的调用点，改成新的接口签名。\n"
    "让我确认一下有没有遗漏的地方。",
]


def _code_body(index: int, lines: int = 26) -> str:
    """造一段「返回了 500 行代码」的假源码。

    :param index: 片段序号（保证内容各不相同）。
    :param lines: 行数。
    :return: 源码文本。
    """
    rows = [
        '    value_{row} = compute("payload-{index}-{row}", {row}, flag=True)'.format(
            row=row, index=index
        )
        for row in range(lines)
    ]
    header = 'def handler_{}(request):\n'.format(index)
    footer = '    # 源码片段结束 {}\n'.format(index)
    return header + "\n".join(rows) + "\n" + footer


def _thinking(index: int, target: str) -> str:
    """造一段絮絮叨叨的思考。

    :param index: 序号。
    :param target: 本步要动的东西。
    :return: 思考正文。
    """
    template = _THINKING_TEMPLATES[index % len(_THINKING_TEMPLATES)]
    return template.format(target=target)


def _turn_plan() -> List[str]:
    """排好 100 步的动作序列（按「回合」分组）。

    每个回合 = 1 段思考 + 若干次**连续**同类动作 ——
    真实 Agent 就是这么干的（一口气读 5 个文件，中间不说话）。

    :return: 100 个动作种类。
    """
    pattern: List[str] = []
    while len(pattern) < 100:
        pattern.extend(["read"] * 5)
        pattern.extend(["exec"] * 1)
        pattern.extend(["edit"] * 4)
        pattern.extend(["read"] * 2)
        pattern.extend(["exec"] * 5)
        pattern.extend(["read"] * 3)
        pattern.extend(["other"] * 1)
        pattern.extend(["edit"] * 5)
        pattern.extend(["exec"] * 2)
        pattern.extend(["read"] * 4)
        pattern.extend(["edit"] * 3)
        pattern.extend(["exec"] * 1)
        pattern.extend(["other"] * 5)
        pattern.extend(["read"] * 2)
        pattern.extend(["edit"] * 3)
    return pattern[:100]


def _build_log() -> List[str]:
    """造一份 100 步、约 200KB 的模拟 Agent 日志（Claude Code 流式格式）。

    :return: JSONL 行列表。
    """
    lines: List[str] = []
    goal = "给旧版鉴权模块补上令牌轮换，并补齐回归测试"
    lines.append(
        json.dumps(
            {"type": "user", "message": {"content": goal}},
            ensure_ascii=False,
        )
    )

    plan = _turn_plan()
    index = 0
    turn = 0
    call_seq = 0
    while index < len(plan):
        kind = plan[index]
        # --- 一个回合：先思考，再连续做几件事 ---
        target = _target_of(kind, index)
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": _thinking(turn, target)}
                        ]
                    },
                },
                ensure_ascii=False,
            )
        )
        turn += 1
        # --- 连续动作 ---
        while index < len(plan) and plan[index] == kind:
            call_seq += 1
            tool_name, tool_input = _tool_of(kind, call_seq)
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_{}".format(call_seq),
                                    "name": tool_name,
                                    "input": tool_input,
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                )
            )
            lines.append(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_{}".format(call_seq),
                                    "content": _code_body(call_seq),
                                    "is_error": False,
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                )
            )
            index += 1
    return lines


def _target_of(kind: str, index: int) -> str:
    """挑一个本步涉及的目标。

    :param kind: 动作种类。
    :param index: 序号。
    :return: 目标文本。
    """
    if kind == "read":
        return _READ_FILES[index % len(_READ_FILES)]
    if kind == "edit":
        return _EDIT_FILES[index % len(_EDIT_FILES)]
    if kind == "exec":
        return _EXEC_COMMANDS[index % len(_EXEC_COMMANDS)]
    return _PATTERN_VALUES[index % len(_PATTERN_VALUES)]


def _tool_of(kind: str, seq: int) -> tuple:
    """造一次工具调用。

    :param kind: 动作种类。
    :param seq: 序号。
    :return: ``(工具名, 参数)``。
    """
    if kind == "read":
        return "Read", {"file_path": _READ_FILES[seq % len(_READ_FILES)]}
    if kind == "edit":
        return "Edit", {"file_path": _EDIT_FILES[seq % len(_EDIT_FILES)]}
    if kind == "exec":
        return "Bash", {"command": _EXEC_COMMANDS[seq % len(_EXEC_COMMANDS)]}
    return "Grep", {"pattern": _PATTERN_VALUES[seq % len(_PATTERN_VALUES)]}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _log() -> List[str]:
    """模拟日志（模块级缓存，避免每个用例都重建）。"""
    if not hasattr(_log, "_cache"):
        _log._cache = _build_log()  # type: ignore[attr-defined]
    return _log._cache  # type: ignore[attr-defined]


def _compressed():
    """压缩后的结果。"""
    return compress_jsonl(_log())


# ---------------------------------------------------------------------------
# 验收：压缩率
# ---------------------------------------------------------------------------
def test_模拟日志本身规模达标():
    """原始日志必须有 100 步、约 200KB —— 否则压缩率无从谈起。"""
    lines = _log()
    size = len("\n".join(lines).encode("utf-8"))
    assert len(lines) > 200, "原始日志行数太少：{}".format(len(lines))
    assert 150 * 1024 <= size <= 320 * 1024, "原始日志大小异常：{} 字节".format(size)


def test_压缩后小于10KB():
    """**核心验收**：200KB 的原始日志必须压进 10KB 以内。"""
    result = _compressed()
    size = len(result.text.encode("utf-8"))
    assert size < MAX_OUTPUT_BYTES, (
        "压缩后仍有 {} 字节（{} 字符），超过 10KB".format(size, result.compressed_chars)
    )


def test_压缩比提升明显():
    """压缩比至少要有一个数量级的提升。"""
    result = _compressed()
    assert result.original_chars > 0
    assert result.ratio < 0.05, "压缩比只有 {:.2%}".format(result.ratio)
    assert result.original_chars > result.compressed_chars * 10


def test_输出行数远少于原始步数():
    """100 步压成几十行。"""
    result = _compressed()
    assert result.output_lines < 120
    assert result.output_lines < len(_log())


# ---------------------------------------------------------------------------
# 铁律一：工具结果只留元数据
# ---------------------------------------------------------------------------
def test_工具结果正文被彻底丢弃():
    """几百行源码不能进事件流 —— 一个字节都不许留。"""
    result = _compressed()
    assert "compute(" not in result.text
    assert "源码片段结束" not in result.text
    assert "def handler_" not in result.text


def test_工具结果保留可核查的元数据():
    """正文丢了，但「做了什么、成功没有」必须留着。"""
    result = _compressed()
    assert "[Read]" in result.text
    assert "成功" in result.text
    # 行数元数据
    assert "行" in result.text


def test_失败回执保留错误摘要():
    """失败的回执要留下「错在哪」，不能只写「失败」。"""
    events = [
        {
            "type": "tool_call",
            "tool": {"name": "Bash", "arguments": {"command": "pytest -q"}},
        },
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "text": "FAILED tests/test_auth.py::test_rotate\nAssertionError",
            "is_error": True,
        },
    ]
    result = compress_events(events)
    assert "失败" in result.text
    assert "FAILED" in result.text


# ---------------------------------------------------------------------------
# 铁律二：连续同类动作合并
# ---------------------------------------------------------------------------
def test_连续读取被合并成一行():
    """连续 5 次 Read 合成一条，不再占 5 行。"""
    result = _compressed()
    assert "[连续读取]" in result.text
    assert "等 5 个文件" in result.text


def test_合并后仍然列得清读过哪些文件():
    """合并不能把「读了什么」也抹掉。"""
    result = _compressed()
    assert "src/core/auth.py" in result.text
    assert "src/core/user.py" in result.text


def test_连续编辑与连续执行同样合并():
    """三大族（读 / 改 / 执行）都要能合并。"""
    result = _compressed()
    assert "[连续编辑]" in result.text
    assert "[连续执行]" in result.text


def test_单个动作不合并():
    """只有 1 次的动作保持原样。"""
    events = [
        {
            "type": "tool_call",
            "tool": {"name": "Read", "arguments": {"file_path": "a.py"}},
        },
        {"type": "tool_result", "text": "x", "is_error": False},
    ]
    result = compress_events(events)
    assert "[连续读取]" not in result.text
    assert "[Read] a.py" in result.text


# ---------------------------------------------------------------------------
# 铁律三：意图提取
# ---------------------------------------------------------------------------
def test_thinking只留首句和关键句():
    """长思考被压成一句话：首句 + 含动词的句子。"""
    text = (
        "嗯，让我先看看这个文件到底是怎么实现的。\n"
        "我想我需要确认一下现有的实现是不是真的有问题。\n"
        "好的，接下来我要修改 src/core/auth.py 里的过期判断逻辑。\n"
        "让我再确认一下刚才那处改动是不是生效了。"
    )
    intent = extract_intent(text)
    assert "修改" in intent
    assert "src/core/auth.py" in intent
    # 语气词与反复确认必须去掉
    assert "让我先看看" not in intent
    assert "确认一下" not in intent
    assert "嗯" not in intent
    assert len(intent) < len(text) / 2


def test_thinking正文不进入事件流():
    """原始思考全文不能出现在压缩结果里。"""
    result = _compressed()
    assert "让我先看看这个文件到底是怎么实现的" not in result.text


def test_短思考原样保留():
    """不超过阈值的短思考不该被砍。"""
    text = "直接修改鉴权逻辑"
    assert extract_intent(text) == text


def test_全是废话时整条丢掉():
    """一句话都没留下时，宁可丢也不能把废话塞进窗口。"""
    assert extract_intent("嗯，好的，哦，是的") == ""
    events = [{"type": "thinking", "text": "嗯，好的，哦，是的"}]
    result = compress_events(events)
    assert "嗯" not in result.text
    assert result.output_lines == 2  # 只有首尾两个锚定行


# ---------------------------------------------------------------------------
# 铁律四：首尾锚定
# ---------------------------------------------------------------------------
def test_首行是任务目标():
    """输出流的第一行永远是 ``[Task Goal]:``。"""
    result = _compressed()
    first = result.text.splitlines()[0]
    assert first.startswith("[Task Goal]: ")
    assert "令牌轮换" in first


def test_末行是当前动作():
    """输出流的最后一行永远是 ``[Current Action]:``。"""
    result = _compressed()
    last = result.text.splitlines()[-1]
    assert last.startswith("[Current Action]: ")
    assert len(last) > len("[Current Action]: ")


def test_显式目标优先于自动提取():
    """调用方给了目标，就用调用方的。"""
    result = compress_jsonl(_log(), options=CompressionOptions(goal="显式目标"))
    assert result.text.splitlines()[0] == "[Task Goal]: 显式目标"


def test_空日志也有锚定():
    """没有日志也不能交白卷。"""
    result = compress_jsonl([])
    lines = result.text.splitlines()
    assert lines[0].startswith("[Task Goal]: ")
    assert lines[-1].startswith("[Current Action]: ")


def test_坏行不影响整体():
    """半截 JSON、脏数据都不能让整条流炸掉。"""
    lines = _log()[:6] + ["{坏掉的 JSON", "", "null", "42"] + _log()[6:]
    result = compress_jsonl(lines)
    assert result.text.splitlines()[0].startswith("[Task Goal]: ")
    assert result.text.splitlines()[-1].startswith("[Current Action]: ")


# ---------------------------------------------------------------------------
# 逻辑链条
# ---------------------------------------------------------------------------
def test_逻辑链条完整():
    """步骤序号递增、目标在首、动作在尾 —— 链条不能断。"""
    result = _compressed()
    lines = result.text.splitlines()

    assert lines[0].startswith("[Task Goal]: ")
    assert lines[-1].startswith("[Current Action]: ")

    numbers = []
    for line in lines[1:-1]:
        head = line.split("]", 1)[0]
        assert head.startswith("[")
        numbers.append(int(head.strip("[]")))
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    # 默认预算下不该有任何省略 —— 序号必须连号
    assert numbers == list(range(1, len(numbers) + 1))


def test_字符预算生效():
    """输出必须落在预算里，锚定行一个都不能少。"""
    options = CompressionOptions(max_chars=1200)
    result = compress_jsonl(_log(), options=options)
    assert len(result.text) <= 1200
    lines = result.text.splitlines()
    assert lines[0].startswith("[Task Goal]: ")
    assert lines[-1].startswith("[Current Action]: ")


def test_预算吃紧时优先砍低价值内容():
    """被砍掉的应该是「说明」「思考」，动作必须留着。"""
    options = CompressionOptions(max_chars=1500)
    result = compress_jsonl(_log(), options=options)
    assert len(result.text) <= 1500
    assert "[连续读取]" in result.text
    assert "[Bash]" in result.text


def test_关键动作没有丢():
    """合并归合并，该留的动作还得留。"""
    result = _compressed()
    assert "pytest tests/test_auth.py -q" in result.text
    assert "src/core/auth.py" in result.text


def test_事件流顺序与原始顺序一致():
    """压缩不能打乱因果顺序。"""
    events = [
        {"type": "thinking", "text": "第一步要读文件"},
        {
            "type": "tool_call",
            "tool": {"name": "Read", "arguments": {"file_path": "first.py"}},
        },
        {"type": "tool_result", "text": "ok", "is_error": False},
        {
            "type": "tool_call",
            "tool": {"name": "Read", "arguments": {"file_path": "second.py"}},
        },
        {"type": "tool_result", "text": "ok", "is_error": False},
    ]
    result = compress_events(events)
    body = result.text.splitlines()
    first_at = next(i for i, line in enumerate(body) if "first.py" in line)
    second_at = next(i for i, line in enumerate(body) if "second.py" in line)
    assert first_at < second_at


def test_统计信息可用():
    """压缩结果带统计，方便看板直接展示。"""
    result = _compressed()
    assert result.input_events > 0
    assert result.original_chars > 0
    assert result.compressed_chars == len(result.text)
    assert result.counts
    payload = result.to_dict()
    assert payload["ratio"] >= 0


# ---------------------------------------------------------------------------
# 文件入口 & 协议兼容
# ---------------------------------------------------------------------------
def test_文件入口(tmp_path: Path):
    """能直接压一个 JSONL 文件。"""
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(_log()), encoding="utf-8")
    result = compress_file(str(path))
    assert len(result.text.encode("utf-8")) < MAX_OUTPUT_BYTES
    assert result.text.splitlines()[0].startswith("[Task Goal]: ")


def test_mcode事件溯源协议():
    """mcode v0.6 的 ``item.*`` 协议也要认。"""
    lines = [
        json.dumps(
            {
                "type": "item.started",
                "item": {
                    "id": "r1",
                    "type": "reasoning",
                    "contentDelta": "接下来我要修改令牌",
                },
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "type": "item.updated",
                "item": {
                    "id": "r1",
                    "type": "reasoning",
                    "contentDelta": "轮换逻辑，并补上测试。",
                },
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "r1",
                    "type": "reasoning",
                    "contentDelta": "",
                },
            },
            ensure_ascii=False,
        ),
    ]
    result = compress_jsonl(lines)
    assert "修改" in result.text
    assert "轮换逻辑" in result.text


def test_不依赖引擎():
    """本模块是独立处理器 —— 不许 import 引擎（免得接入前就搅在一起）。"""
    source = Path(__file__).resolve().parents[1] / "src" / "core" / "log_compressor.py"
    content = source.read_text(encoding="utf-8")
    assert "import engine" not in content
    assert "from ..core.engine" not in content
    assert "engine" not in [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def test_纯函数不碰磁盘():
    """压缩事件流是纯函数 —— 只吃事件、只吐文本。"""
    payload: Dict[str, Any] = {
        "type": "tool_call",
        "tool": {"name": "Read", "arguments": {"file_path": "a.py"}},
    }
    before = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    compress_events([payload])
    after = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert before == after
