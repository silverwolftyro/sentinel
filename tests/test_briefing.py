"""晨报模块（v1.2.0）的单元测试。

重点验证三件事：

1. **统计准不准** —— 红线次数、漂移次数、``[STUCK]`` 次数、挽回的 Token；
2. **Markdown 渲染** —— 该有的条目一个不少，不该有的不能瞎写；
3. **落盘** —— 文件名格式对、内容对、同天重跑是覆盖而不是堆积。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.briefing import (
    BriefingStats,
    _extract_sections,
    briefing_filename,
    collect_from_engine,
    default_archive_dir,
    format_token_count,
    render_markdown,
    render_terminal,
    save_briefing,
)


class _FakeLedger:
    """假的 Token 台账。"""

    def __init__(self, snapshot: Dict[str, Any]) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._snapshot)


class _FakeTracker:
    """假的意图追踪器。"""

    def __init__(self, goal: str = "重构 auth 模块") -> None:
        self.goal = goal


class _FakeRouter:
    """假的 L2 路由器。"""

    def __init__(self) -> None:
        self.models_used = {
            "ollama:qwen2.5:7b": 9,
            "openai_compatible:deepseek-chat": 4,
        }
        self.escalations = [
            {
                "reason": "red_line",
                "model": "openai_compatible:deepseek-chat",
                "at": time.time() - 60,
            }
        ]


class _FakeEngine:
    """假的引擎（只提供晨报需要的那些属性）。"""

    def __init__(self) -> None:
        self.agent_name = "codex"
        self.started_at = time.time() - 3600
        self.ended_at = time.time()
        self.processed_events = 412
        self.counts = {"ON_TRACK": 386, "DANGEROUS": 12, "LOOP": 3}
        self.token_ledger = _FakeLedger(
            {
                "total_tokens": 247153,
                "estimated_tokens": 0,
                "estimated": False,
                "events": 40,
                "avg_per_event": 40.0,
            }
        )
        self.intent_tracker = _FakeTracker()
        self.llm_client = _FakeRouter()
        self.alerts: List[Dict[str, Any]] = [
            {
                "verdict": "DANGEROUS",
                "level": "critical",
                "rule_id": "catastrophic_mass_delete",
                "reason": "红线级命中：用 find 批量删除文件",
                "severity": "red_line",
                "timestamp": time.time() - 300,
                "explanation": "批量删除临时文件。",
                "token_saved": {"tokens": 24800},
            },
            {
                "verdict": "STUCK",
                "level": "warn",
                "rule_id": "context_stuck",
                "reason": "[STUCK] 测试连续失败3次",
                "severity": "standard",
                "timestamp": time.time() - 200,
                "explanation": "Agent 连续三次跑 pytest 都失败。",
            },
            {
                "verdict": "DRIFT",
                "level": "warn",
                "rule_id": "intent_drift",
                "reason": "[WARNING] 意图漂移",
                "severity": "standard",
                "timestamp": time.time() - 100,
            },
            {
                "verdict": "DANGEROUS",
                "level": "warn",
                "rule_id": "dangerous_command",
                "reason": "危险命令",
                "severity": "standard",
                "timestamp": time.time() - 50,
            },
        ]


class TestCollectFromEngine:
    """从引擎采集统计。"""

    def test_should_count_verdicts(self) -> None:
        """红线 / 漂移 / STUCK 都要分开数清楚。"""
        stats = collect_from_engine(_FakeEngine())
        assert stats.red_line_count == 1
        assert stats.drift_count == 1
        assert stats.stuck_count == 1
        assert stats.loop_count == 0

    def test_should_sum_token_saved(self) -> None:
        """挽回的 Token 要累加。"""
        stats = collect_from_engine(_FakeEngine())
        assert stats.token_saved == 24800

    def test_should_read_tokens_and_models(self) -> None:
        """Token 与「用过哪些模型」都要采到。"""
        stats = collect_from_engine(_FakeEngine())
        assert stats.total_tokens == 247153
        assert stats.token_estimated is False
        assert stats.models_used["ollama:qwen2.5:7b"] == 9
        assert len(stats.escalations) == 1

    def test_should_read_goal_from_tracker(self) -> None:
        """任务目标从意图追踪器里取。"""
        stats = collect_from_engine(_FakeEngine())
        assert stats.task_goal == "重构 auth 模块"

    def test_should_tolerate_none_engine(self) -> None:
        """引擎没起来也不能崩（晨报绝不影响主流程）。"""
        stats = collect_from_engine(None)
        assert stats.total_tokens == 0
        assert stats.alerts == []


class TestRenderMarkdown:
    """Markdown 渲染。"""

    @pytest.fixture()
    def markdown(self) -> str:
        return render_markdown(collect_from_engine(_FakeEngine()))

    def test_should_contain_required_sections(self, markdown: str) -> None:
        """任务要求的条目一个都不能少。"""
        for key in (
            "执行时长",
            "总 Token 消耗",
            "红线级拦截",
            "意图漂移",
            "`[STUCK]` 卡死",
            "预估挽回的无效消耗",
            "本次用到的模型",
        ):
            assert key in markdown, f"晨报里少了「{key}」"

    def test_should_contain_numbers(self, markdown: str) -> None:
        """数字要对。"""
        assert "24.7 万" in markdown
        assert "24800" in markdown or "2.5 万" in markdown

    def test_should_list_models(self, markdown: str) -> None:
        """用过的模型要列出来。"""
        assert "ollama:qwen2.5:7b" in markdown
        assert "openai_compatible:deepseek-chat" in markdown

    def test_should_mark_escalation(self, markdown: str) -> None:
        """升级到强模型的时刻要写清楚。"""
        assert "红线级拦截" in markdown

    def test_should_handle_no_alert(self) -> None:
        """整晚零告警也要出一份像样的晨报。"""
        stats = BriefingStats(session_id="s1", agent="codex")
        markdown = render_markdown(stats)
        assert "零告警" in markdown
        assert "没有调用任何模型" in markdown

    def test_should_not_claim_estimation_is_exact(self) -> None:
        """估算值必须标出来（不能冒充真值）。"""
        stats = BriefingStats(total_tokens=539, token_estimated=True)
        assert "~539" in render_markdown(stats)


class TestRenderTerminal:
    """终端摘要。"""

    def test_should_contain_key_lines(self) -> None:
        """终端摘要要能一眼看完。"""
        text = render_terminal(collect_from_engine(_FakeEngine()))
        assert "哨兵 · 夜间值守晨报" in text
        assert "红线拦截" in text
        assert "预估挽回" in text


class TestFormatTokenCount:
    """Token 数量渲染。"""

    def test_should_use_wan_for_large(self) -> None:
        assert format_token_count(247153) == "24.7 万"

    def test_should_mark_estimated(self) -> None:
        assert format_token_count(539, estimated=True) == "~539"

    def test_should_keep_small_exact(self) -> None:
        assert format_token_count(42) == "42"

    def test_should_handle_zero(self) -> None:
        assert format_token_count(0) == "0"


class TestSaveBriefing:
    """落盘。"""

    def test_should_use_dated_filename(self, tmp_path: Any) -> None:
        """文件名格式：晨报-YYYY-MM-DD.md。"""
        path = save_briefing("# 内容\n", archive_dir=str(tmp_path))
        name = os.path.basename(path)
        assert name.startswith("晨报-")
        assert name.endswith(".md")
        assert len(name) == len("晨报-2026-01-01.md")

    def test_should_write_content(self, tmp_path: Any) -> None:
        """内容要真的写进去。"""
        path = save_briefing("# 标题\n正文\n", archive_dir=str(tmp_path))
        with open(path, "r", encoding="utf-8") as handle:
            assert "正文" in handle.read()

    def test_should_overwrite_same_day(self, tmp_path: Any) -> None:
        """同一天重跑是覆盖，不是堆积。"""
        first = save_briefing("# 第一次\n", archive_dir=str(tmp_path))
        second = save_briefing("# 第二次\n", archive_dir=str(tmp_path))
        assert first == second
        with open(second, "r", encoding="utf-8") as handle:
            assert "第二次" in handle.read()
        assert len(os.listdir(str(tmp_path))) == 1

    def test_should_create_archive_dir(self, tmp_path: Any) -> None:
        """目录不存在要自动建。"""
        target = tmp_path / "晨报归档"
        path = save_briefing("# 内容\n", archive_dir=str(target))
        assert os.path.isdir(str(target))
        assert os.path.exists(path)

    def test_default_archive_dir_mentions_archive(self) -> None:
        """默认归档目录名要对。"""
        assert default_archive_dir().endswith(
            os.path.join("文档与报告", "晨报归档")
        )

    def test_filename_uses_given_time(self) -> None:
        """文件名跟着传入的时间走。"""
        from datetime import datetime

        name = briefing_filename(datetime(2026, 3, 7, 12, 0))
        assert name == "晨报-2026-03-07.md"


class TestExtractSections:
    """分节解析（每日合并日报的基石）。"""

    def test_should_parse_two_sections(self) -> None:
        """两路会话要被解析成两个分节。"""
        content = (
            "# 哨兵值守晨报 · 2026-10-27\n\n"
            "<!-- sentinel-session:a -->\n## A\n\nA 的内容\n"
            "<!-- /sentinel-session:a -->\n\n---\n\n"
            "<!-- sentinel-session:b -->\n## B\n\nB 的内容\n"
            "<!-- /sentinel-session:b -->\n"
        )
        sections = _extract_sections(content)
        assert len(sections) == 2
        assert sections[0].startswith("<!-- sentinel-session:a -->")
        assert sections[1].startswith("<!-- sentinel-session:b -->")
        assert "A 的内容" in sections[0]
        assert "B 的内容" in sections[1]

    def test_should_discard_legacy_content(self) -> None:
        """旧格式残留（没有标记的内容）要被丢掉。"""
        content = (
            "# 哨兵值守晨报\n\n## 旧版正文\n\n没有标记的残留\n\n"
            "<!-- sentinel-session:a -->\n## A\n\nA 的内容\n"
            "<!-- /sentinel-session:a -->\n"
        )
        sections = _extract_sections(content)
        assert len(sections) == 1
        assert "旧版正文" not in sections[0]

    def test_should_return_empty_for_garbage(self) -> None:
        """空内容或乱内容都不该抛异常。"""
        assert _extract_sections("") == []
        assert _extract_sections("随便一段文字") == []


class TestDailyMerge:
    """多路会话的「每日合并日报」。"""

    def test_should_merge_two_sessions(self, tmp_path: Any) -> None:
        """两路合到同一份文件里，不能互相覆盖。"""
        first = save_briefing(
            "## 会话 A\n\nA 的内容\n",
            archive_dir=str(tmp_path),
            session_id="stress-a",
        )
        second = save_briefing(
            "## 会话 B\n\nB 的内容\n",
            archive_dir=str(tmp_path),
            session_id="stress-b",
        )
        assert first == second
        with open(second, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert "A 的内容" in content
        assert "B 的内容" in content
        assert content.count("# 哨兵值守晨报") == 1

    def test_should_replace_same_session(self, tmp_path: Any) -> None:
        """同一路重跑要**替换**自己那一节，不堆叠。"""
        path = save_briefing(
            "## 会话 A\n\n第一次\n",
            archive_dir=str(tmp_path),
            session_id="stress-a",
        )
        save_briefing(
            "## 会话 A\n\n第二次\n",
            archive_dir=str(tmp_path),
            session_id="stress-a",
        )
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert "第二次" in content
        assert "第一次" not in content
        assert content.count("会话 A") == 1

    def test_single_session_overwrites(self, tmp_path: Any) -> None:
        """单路模式（不传 session_id）就是直接覆盖。"""
        path = save_briefing("# 第一次\n", archive_dir=str(tmp_path))
        save_briefing("# 第二次\n", archive_dir=str(tmp_path))
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert "第二次" in content
        assert "第一次" not in content


class TestBriefingDemoScript:
    """归档脚本的用法约定。

    ⚠️ 库本身没问题 —— ``save_briefing()`` 不传 ``session_id``
    就是「单路覆盖」，传了就是「合并日报」。
    实机归档时**踩过**：脚本漏传 ``session_id``，
    结果把同一天已有的三节日报**整个覆盖掉**了
    （v1.2.0 的 stress-a / stress-b 两节直接没了）。
    所以这里加一条针对**脚本**的回归。
    """

    def _script_source(self) -> str:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(
            root, "_构建与发布", "scripts", "v21_briefing_demo.py"
        )
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_should_pass_session_id_when_archiving(self) -> None:
        """归档时必须传 session_id，否则会覆盖整份日报。"""
        source = self._script_source()
        assert "session_id=SESSION_ID" in source

    def test_should_use_section_heading_level(self) -> None:
        """标题层级要跟启动器一致（heading_level=2）。"""
        source = self._script_source()
        assert "heading_level=2" in source


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
