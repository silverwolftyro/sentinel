"""v3.8.0 回归 —— 前后端解析一致性（跨语言对拍）。

背景
====

v3.7.0 留了个**真盲区**：Claude 风格的**同一行**事件里，
``thinking`` 块排在 ``tool_use`` 块**前面**时，
看板的日志解析会先命中思考块，**工具调用整个被顶掉** ——
表现就是「日志里有工具名，看板却解析不出来」。

同时还有反向的错位：后端适配器认得 ``tool-call`` / ``toolName`` /
``functionCall`` 这些形态，前端认不得；
前端认得 ``{"tool":{…}}`` 这种嵌套，后端认不得。
两边只要有一边不认识某个形态，就会出现「看板解析出来了、后端却当没这回事」。

本模块做什么
============

拿**同一批**原始日志行，分别喂给：

* **后端**：:class:`src.adapters.mcode.McodeAdapter` 与
  :class:`src.adapters.claude.ClaudeAdapter`（引擎真正用的两个适配器）；
* **前端**：``tests/fixtures/v380_frontend_harness.js --dump``
  （把 dashboard.js 真的跑起来，吐出每行的解析结果）。

然后逐行**对拍** —— 这不是「文件里有没有这个字符串」那种弱断言，
是两边真的把同一行日志解析一遍再比结果。

判定规则
========

* **强规则**：后端解析出了工具调用 ⇒ 前端**必须**也解析出工具调用，
  而且工具名要对得上（占位符除外，见下）。
* **例外**：前端解析出了工具调用、后端却没解析出来的行 ——
  只允许出现在「参数还没出现」的行上（mcode v0.6 的 ``item.started``）。
  后端是**故意**等参数第一次出现才产事件的（那才是工具执行前的瞬间），
  前端更早显示工具名不算错，但这类行必须**白名单**登记，不许悄悄放过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from src.adapters.claude import ClaudeAdapter
from src.adapters.mcode import McodeAdapter

#: 仓库根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 前后端共用的原始日志行（JSONL）。
FIXTURE_PATH = os.path.join(
    ROOT, "tests", "fixtures", "v380_parse_consistency.jsonl"
)

#: 前端夹具脚本。
HARNESS_PATH = os.path.join(
    ROOT, "tests", "fixtures", "v380_frontend_harness.js"
)

#: 占位符归一化 —— 两边的「无名工具」叫法不一样，但**含义**一样。
_NAME_PLACEHOLDER = {
    "未命名工具": "unknown",
}

#: 允许「前端比后端更早显示工具名」的行（按原始日志行索引）。
#:
#: ⚠️ 只有 mcode v0.6 的 ``item.started``（参数还没出现）属于这一类 ——
#:    后端刻意等参数第一次出现才产 ``tool_call`` 事件。
_FRONTEND_EARLY_ALLOWED_INDEXES = frozenset({3})


def _read_fixture_lines() -> list[str]:
    """读出共享夹具里的原始日志行。

    :return: 去掉空行后的原始行列表。
    """
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        return [
            line.strip()
            for line in handle.read().splitlines()
            if line.strip()
        ]


def _dump_frontend(lines: list[str], tmp_path) -> list[dict]:
    """让前端夹具把同一批行解析一遍，返回结构化结果。

    ⚠️ 子进程输出**写文件**而不是接管道 ——
        免得输出量大时被卡住，也免得控制台编码把 JSON 搅坏。

    :param lines: 原始日志行。
    :param tmp_path: pytest 提供的临时目录。
    :return: 每行的 ``{raw, kind, tool, action, target}``。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没装 Node，跳过前后端对拍")

    source = os.path.join(tmp_path, "input.jsonl")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    out_path = os.path.join(tmp_path, "dump.json")
    err_path = os.path.join(tmp_path, "dump.err")
    with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
        completed = subprocess.run(
            [node, HARNESS_PATH, "--dump", source],
            stdout=out_file,
            stderr=err_file,
            cwd=ROOT,
            check=False,
        )

    with open(err_path, "rb") as err_file:
        stderr_text = err_file.read().decode("utf-8", errors="replace")
    assert completed.returncode == 0, f"前端夹具退出码异常：{stderr_text}"

    with open(out_path, "rb") as out_file:
        raw_text = out_file.read().decode("utf-8", errors="replace")
    return json.loads(raw_text)


def _backend_tool_names(raw: str) -> set[str]:
    """后端两个适配器解析出的工具名集合。

    :param raw: 一行原始日志。
    :return: 工具名集合（空集合表示后端没把它当成工具调用）。
    """
    names: set[str] = set()
    for adapter_cls in (McodeAdapter, ClaudeAdapter):
        # ⚠️ 每个适配器**按行新建** ——
        #    mcode v0.6 的去重状态是跨行的（``_seen_calls``），
        #    而看板也是逐行独立解析的，这里必须对齐口径。
        adapter = adapter_cls()
        for event in adapter.parse_line(raw):
            if event.get("type") != "tool_call":
                continue
            tool = event.get("tool")
            name = str((tool or {}).get("name") or "")
            if name:
                names.add(name)
    return names


def _normalize(name: str) -> str:
    """把两边的占位符归一化成同一个说法。

    :param name: 工具名。
    :return: 归一化后的工具名。
    """
    return _NAME_PLACEHOLDER.get(name, name)


@pytest.fixture(scope="module")
def parsed_rows(tmp_path_factory) -> list[dict]:
    """前后端对拍的逐行结果。

    :param tmp_path_factory: pytest 的模块级临时目录工厂。
    :return: 每行的前端解析结果。
    """
    lines = _read_fixture_lines()
    return _dump_frontend(lines, tmp_path_factory.mktemp("v380_dump"))


class Test前后端解析一致:
    """同一批原始日志行，前后端必须解析出同样的工具调用。"""

    def test_共享夹具不是空的(self, parsed_rows: list[dict]) -> None:
        """夹具本身得有几行 —— 空夹具等于没测。"""
        assert len(parsed_rows) == 13

    def test_后端解析出工具的行_前端必须也解析出(self, parsed_rows: list[dict]) -> None:
        """强规则：后端看到的工具调用，前端不许看不到。"""
        lines = _read_fixture_lines()
        mismatches: list[str] = []
        for index, row in enumerate(parsed_rows):
            backend = _backend_tool_names(lines[index])
            if not backend:
                continue
            if row["kind"] != "tool":
                mismatches.append(
                    f"第 {index + 1} 行：后端认出工具 {sorted(backend)}，"
                    f"前端却判成 {row['kind']}"
                )
                continue
            if _normalize(row["tool"]) not in backend:
                mismatches.append(
                    f"第 {index + 1} 行：后端工具名 {sorted(backend)}，"
                    f"前端 {row['tool']}"
                )
        assert not mismatches, "前后端解析不一致：\n" + "\n".join(mismatches)

    def test_前端多认出来的行_必须在白名单里(
        self, parsed_rows: list[dict]
    ) -> None:
        """例外规则：前端更早显示工具名的行，必须登记在白名单里。"""
        lines = _read_fixture_lines()
        unexpected: list[str] = []
        for index, row in enumerate(parsed_rows):
            if row["kind"] != "tool":
                continue
            if _backend_tool_names(lines[index]):
                continue
            if index in _FRONTEND_EARLY_ALLOWED_INDEXES:
                continue
            unexpected.append(
                f"第 {index + 1} 行：前端认出工具 {row['tool']}，"
                "后端却没有 —— 这行必须登记进白名单"
            )
        assert not unexpected, "未登记的前后端差异：\n" + "\n".join(unexpected)

    def test_工具回执不被误当成工具调用(self, parsed_rows: list[dict]) -> None:
        """``tool_result`` 是回执，不是调用 —— 两边都不能当成工具调用。"""
        lines = _read_fixture_lines()
        index = 7  # Claude user + tool_result
        assert '"tool_result"' in lines[index]
        assert not _backend_tool_names(lines[index])
        assert parsed_rows[index]["kind"] != "tool"


class TestClaude同行工具调用:
    """★ v3.8.0 的核心验收：Claude 同行 thinking + tool_use。"""

    def test_后端能解析出工具名(self) -> None:
        """后端适配器本来就对（逐块解析）—— 这里守住它别退化。"""
        raw = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "先读一下配置"},
                        {
                            "type": "tool_use",
                            "id": "toolu_v380_1",
                            "name": "Read",
                            "input": {"file_path": "core/auth.py"},
                        },
                    ],
                },
            },
            ensure_ascii=False,
        )
        assert _backend_tool_names(raw) == {"Read"}

    def test_前端也能解析出工具名(self, parsed_rows: list[dict]) -> None:
        """★ 盲区修复的落点：前端不许再把工具调用顶掉。"""
        index = 4  # 共享夹具第 5 行
        row = parsed_rows[index]
        assert row["kind"] == "tool"
        assert row["tool"] == "Read"
        assert row["target"] == "core/auth.py"
        assert row["action"] == "read"

    def test_前后端解析结果完全一致(self, parsed_rows: list[dict]) -> None:
        """★ 同一行，前后端认出的工具名与目标必须一致。"""
        lines = _read_fixture_lines()
        index = 4
        backend = _backend_tool_names(lines[index])
        row = parsed_rows[index]
        assert backend == {_normalize(row["tool"])}


class TestMcode工具调用:
    """mcode 两代协议 + 三个嵌套形态，前后端都要认得。"""

    @pytest.mark.parametrize(
        ("index", "expected_tool"),
        [
            (0, "Read"),   # 旧协议：{"type":"tool_call","tool_call":{…}}
            (1, "Write"),  # tool 嵌套：{"type":"tool_call","tool":{…}}
            (2, "Read"),   # v0.6：item.toolCall.input
            (10, "Grep"),  # tool-call（带连字符）
            (11, "Glob"),  # toolName 嵌套
            (12, "Shell"),  # functionCall 嵌套
        ],
    )
    def test_前后端都认得(
        self, parsed_rows: list[dict], index: int, expected_tool: str
    ) -> None:
        """这些形态前后端必须解析出**同一个**工具名。"""
        lines = _read_fixture_lines()
        backend = _backend_tool_names(lines[index])
        row = parsed_rows[index]
        assert backend == {expected_tool}, f"后端：{backend}"
        assert row["kind"] == "tool"
        assert row["tool"] == expected_tool
