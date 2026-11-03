"""v3.5.1 回归：日志跟随锁定 · 自然语言流水三色 · 空态引导 · 呼吸灯。

背景
====

v3.5.0 把**内核**做到了位（多流隔离、级联过滤、极速缓存），
但**前端呈现**还有四个「反人类」的坑：

1. 雷达空荡荡的 —— 用户**不知道该去怎么启动日志**，
   光写一句「未发现」等于把人晾在原地；
2. 右侧「被监控日志」**强制锁定在最底部** ——
   用户想回看刚那段历史，滚上去两秒又被新日志拽回来，
   根本没法看；
3. 中间的「实时审核流水」全是 ``[TAIL] #6 thinking | …`` 这种机器码，
   用户不知道 Agent 到底在干嘛；
4. 缺乏动态视觉反馈 —— 不知道系统是不是还活着。

本文件把这四件事**钉住**，一条都不许退化。

与 v3.5.0 的区别
==============

这一版的测试**不只**查「文件里有没有这个字符串」：

* 前端的「拼人话」逻辑用 Node 的 ``vm`` 沙箱**真的跑一遍**
  （``tests/fixtures/v351_frontend_harness.js``），
  断言的是**行为**而不是文本；
* 后端的摘要提取（``extract_snippet``）是**纯 Python**，
  可以直接断言输出。

⚠️ Node 不在时那一条**跳过**而不是失败 ——
   Node 不是产品的运行依赖，不能因为它缺席就红。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.engine import ONTRACK_MARKER, SentinelEngine
from src.core.live_audit import (
    MAX_SNIPPET_LENGTH,
    extract_snippet,
    format_clock,
)
from src.core.session_server import (
    ALERT_JSON_MARKER,
    AlertStore,
    EventHub,
    Watcher,
    parse_ontrack_line,
)

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _write_jsonl(path, lines) -> str:
    """写一份 JSONL 日志并返回路径。

    :param path: 目标路径。
    :param lines: 载荷列表。
    :return: 路径字符串。
    """
    body = "\n".join(
        json.dumps(line, ensure_ascii=False) for line in lines
    )
    path.write_text(body + "\n", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# 一、后端：把「人话要引用的原文」摘出来
# ---------------------------------------------------------------------------
class TestExtractSnippet:
    """``extract_snippet`` —— 从事件里抽出前 40 个字符的原文。"""

    def test_思考事件取正文(self):
        """thinking → 思考正文。"""
        snippet = extract_snippet({
            "type": "thinking",
            "text": "关键发现是鉴权中间件没有校验 refresh token",
        })
        assert snippet == "关键发现是鉴权中间件没有校验 refresh token"

    def test_思考事件按四十字截断(self):
        """太长 → 截到 40 个字符（含省略号）。"""
        snippet = extract_snippet({
            "type": "thinking",
            "text": "一" * 120,
        })
        assert len(snippet) == MAX_SNIPPET_LENGTH
        assert snippet.endswith("…")

    def test_工具调用取命令行(self):
        """tool_call → 命令行本身。"""
        snippet = extract_snippet({
            "type": "tool_call",
            "tool": {
                "name": "Bash",
                "arguments": {"command": "pytest -q tests/"},
                "command": "pytest -q tests/",
            },
        })
        assert snippet == "pytest -q tests/"

    def test_没有命令行就取文件路径(self):
        """读文件类工具 → 退到「动了哪个文件」。"""
        snippet = extract_snippet({
            "type": "tool_call",
            "tool": {
                "name": "Read",
                "arguments": {"file_path": "D:\\work\\src\\core\\auth.py"},
                "command": "",
            },
        })
        assert snippet == "core/auth.py"

    def test_什么都没有就取工具名(self):
        """认不出来的工具 → 至少报出工具名。"""
        snippet = extract_snippet({
            "type": "tool_call",
            "tool": {"name": "mcp__weird__thing", "arguments": {}, "command": ""},
        })
        assert snippet == "mcp__weird__thing"

    def test_文本事件也取正文(self):
        """text 事件 → 同样取正文。"""
        assert extract_snippet({
            "type": "text",
            "text": "已经修好了登录 bug",
        }) == "已经修好了登录 bug"

    def test_坏事件不崩(self):
        """非法输入 → 空串（架构红线 R5：审查侧异常一律降级）。"""
        assert extract_snippet(None) == ""
        assert extract_snippet({}) == ""
        assert extract_snippet({"type": "tool_call", "tool": None}) == ""
        assert extract_snippet({"type": "result"}) == ""

    def test_摘要里绝不出现JSON(self):
        """流水区**不显示完整 JSON** —— 摘要是干净的一行字。"""
        snippet = extract_snippet({
            "type": "tool_call",
            "tool": {
                "name": "Bash",
                "arguments": {"command": "ls -la", "extra": {"a": 1}},
                "command": "ls -la",
            },
        })
        assert "{" not in snippet
        assert "}" not in snippet
        assert "\n" not in snippet


# ---------------------------------------------------------------------------
# 二、日志格式契约：引擎写 → 看板读
# ---------------------------------------------------------------------------
class TestOnTrackPayload:
    """流水行里必须**带着** snippet —— 前端才拼得出人话。"""

    def test_引擎写出的流水行带snippet(self, tmp_path):
        """ONTRACK_JSON 里要有 snippet 字段。"""
        log = _write_jsonl(tmp_path / "stream.jsonl", [
            {"type": "tool_call", "tool_call": {
                "name": "Bash", "arguments": {"command": "pytest -q"}}},
        ])
        alert_log = tmp_path / "sentinel_alert.log"

        from src.adapters.mcode import McodeAdapter
        from src.rules.basic import RuleEngine

        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine(config={}),
            alert_log_path=str(alert_log),
            console=False,
        )
        try:
            engine.run(log)
        finally:
            engine.close()

        content = alert_log.read_text(encoding="utf-8")
        assert ONTRACK_MARKER in content
        entries = [
            parse_ontrack_line(line) for line in content.splitlines()
        ]
        entries = [item for item in entries if item is not None]
        assert entries
        assert entries[0]["snippet"] == "pytest -q"

    def test_解析器透传snippet(self):
        """``parse_ontrack_line`` 原样返回载荷 —— snippet 不能被吃掉。"""
        entry = parse_ontrack_line(
            ONTRACK_MARKER + " " + json.dumps(
                {
                    "summary": "正常执行: pytest",
                    "snippet": "pytest -q tests/",
                    "timestamp": 1793032000.5,
                },
                ensure_ascii=False,
            )
        )
        assert entry is not None
        assert entry["snippet"] == "pytest -q tests/"


# ---------------------------------------------------------------------------
# 三、前端：跟随锁定 + 三色 + 空态引导 + 呼吸灯
# ---------------------------------------------------------------------------
class TestFrontendAssets:
    """看板必须真的把新东西露出来。"""

















# ---------------------------------------------------------------------------
# 三·二、告警必须**权威地**进流水快照
# ---------------------------------------------------------------------------
class TestAlertEntersAuditStream:
    """告警也得进流水区 —— 而且必须由**服务端**进。

    为什么必须是服务端
    ==================

    看板每 15 秒会用服务端快照**整段重画**流水区
    （``renderLiveAudit`` → ``clearLiveAudit``）。
    前端临时追加的告警行会被那次重画**整个抹掉** ——
    于是「出事的那一刻」在流水区等于**从来没出现过**。

    所以告警必须由服务端进快照：
    刷新页面、切会话都还在。
    """

    def test_告警行进快照(self, tmp_path):
        """告警日志里出现 ALERT_JSON → 快照里多一条告警流水。"""
        watch = tmp_path / "watch.jsonl"
        watch.write_text("", encoding="utf-8")
        alert_log = tmp_path / "alert.log"
        alert_log.write_text("", encoding="utf-8")

        hub = EventHub()
        store = AlertStore()
        watcher = Watcher(
            str(watch), str(alert_log), hub, store, session_id="s1"
        )

        with alert_log.open("a", encoding="utf-8") as handle:
            handle.write(
                ALERT_JSON_MARKER
                + " "
                + json.dumps(
                    {
                        "alert_id": "a-1",
                        "timestamp": 1793032000.5,
                        "verdict": "DANGEROUS",
                        "level": "critical",
                        "severity": "red_line",
                        "reason": "红线级命中：递归删除根目录 / 家目录 / 整盘（/）",
                        "command": "rm -rf /",
                        "tool": "Bash",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        watcher._poll_once()

        snapshot = store.snapshot(str(watch), str(alert_log))
        entries = [
            item
            for item in snapshot["ontrack"]
            if item.get("stream_kind") == "alert"
        ]
        assert len(entries) == 1
        assert entries[0]["verdict"] == "DANGEROUS"
        assert entries[0]["command"] == "rm -rf /"
        assert entries[0]["time"] == format_clock(1793032000.5)

    def test_实时推的告警也带标记(self, tmp_path):
        """SSE 推来的告警事件也带 ``stream_kind`` —— 前端据此认它。"""
        watch = tmp_path / "watch.jsonl"
        watch.write_text("", encoding="utf-8")
        alert_log = tmp_path / "alert.log"
        alert_log.write_text("", encoding="utf-8")

        hub = EventHub()
        queue = hub.subscribe()
        store = AlertStore()
        watcher = Watcher(
            str(watch), str(alert_log), hub, store, session_id="s1"
        )

        with alert_log.open("a", encoding="utf-8") as handle:
            handle.write(
                ALERT_JSON_MARKER
                + " "
                + json.dumps(
                    {
                        "alert_id": "a-1",
                        "timestamp": 1793032000.5,
                        "verdict": "LOOP",
                        "level": "warn",
                        "severity": "standard",
                        "reason": "连续 3 次读取同一个文件，疑似死循环: auth.py",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        watcher._poll_once()

        assert queue.qsize() == 1
        event = queue.get_nowait()
        assert event["type"] == "alert"
        assert event["stream_kind"] == "alert"
        assert event["verdict"] == "LOOP"

    def test_告警流水不重复计入正常数(self):
        """「X 告警 / Y 正常」里的 Y 不能把告警再数一遍。"""
        store = AlertStore()
        store.add_alert({
            "alert_id": "a-1",
            "fingerprint": "thread:A",
            "timestamp": 1.0,
        })
        store.add_ontrack({
            "summary": "正常读取",
            "fingerprint": "thread:A",
            "timestamp": 1.0,
        })
        store.add_ontrack({
            "stream_kind": "alert",
            "verdict": "DANGEROUS",
            "fingerprint": "thread:A",
            "timestamp": 2.0,
        })

        streams = {item["key"]: item for item in store.streams()}
        assert streams["thread:A"]["alerts"] == 1
        assert streams["thread:A"]["ontrack"] == 1


# ---------------------------------------------------------------------------
# 四、前端逻辑：**真的跑一遍**（Node 沙箱）
# ---------------------------------------------------------------------------
class TestFrontendBehaviour:
    """用 Node 把 dashboard.js 跑起来，断言它的**行为**。"""



# ---------------------------------------------------------------------------
# 五、版本口径
# ---------------------------------------------------------------------------
def test_版本号跟当前包版本一致():
    """版本号必须跟当前包版本一致 —— 这里钉住其中一处。

    ⚠️ v3.5.2 起改成**动态**断言 —— 以前这里写死了 ``3.5.1``，
    每发一次版就得改一遍，而「忘了改」正是版本号不同步的**头号原因**
    （v3.0.1 起 ``test_v300_release.py`` 就已经全面动态化，
    v3.3.2 与 v3.4.0 的测试文件也都已经跟上）。
    """
    import src

    source = _read("src", "__init__.py")
    assert '"{}"'.format(src.__version__) in source
    assert (ROOT / "CHANGELOG.md").exists()
