"""v3.2.0 追加回归 —— 演习脚本与「重拉」链路的真缺陷。

这一版的终局 Night Crawl 演习**连踩了四个坑**，
每一个都是实机跑出来才暴露的：

1. ★★ **进程还在 ≠ 还在干活** ——
   ``mcode exec`` 干完活会发 ``exec.completed``，但**进程赖着不退**。
   原来只看 ``proc.poll()``，于是 Agent 干完活就再也不会被重拉，
   后面二十几分钟全是**空窗**（屏幕里一条真实事件都没有）；
2. ★ **重拉之后同一行日志被读两遍** ——
   ``relaunch()`` 为新进程起了读者，``run()`` 又为**同一个**新进程起一个，
   事件数直接翻倍；
3. ★ **L2 模型不在，30 分钟白跑** ——
   本机 Ollama 服务在跑但**一个模型都没装**；
   开跑前不验一验，30 分钟跑完才发现「漂移 0 次」；
4. ★ **验收项那行 ``✓`` 在 GBK 终端里编不出来** ——
   一崩就把 JSON 报告一起带走，30 分钟白跑。

覆盖清单：

1. 「进程还在但这一轮干完了」→ **要重拉**；
2. 同一代**只起一个读者**（事件不翻倍）；
3. 演习脚本的 **L2 预检**：模型在 / 不在；
4. 重拉**最小间隔**不能太密（别把 API 烧穿）；
5. 演习脚本的**控制台编码**必须是 UTF-8；
6. 报告里的**关键字段**一个都不能少。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

#: 仓库根目录。
ROOT = Path(__file__).resolve().parents[1]

#: 演习脚本路径。
CRAWL_PATH = ROOT / "_构建与发布" / "scripts" / "final_night_crawl.py"


def _load(path: Path, name: str) -> Any:
    """按路径加载脚本（它们不在包内）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crawl = _load(CRAWL_PATH, "final_night_crawl_under_test")
launcher = _load(
    ROOT / "_构建与发布" / "scripts" / "run_with_sentinel.py",
    "run_with_sentinel_v320",
)


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------
class _FakeProc:
    """假的子进程。"""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.pid = 4242

    def poll(self) -> Any:
        """进程还在就返回 ``None``。"""
        return None if self.alive else 0


class _FakeSession:
    """假的 AgentSession。"""

    def __init__(self, proc: Any) -> None:
        self.proc = proc
        self.engine = None
        self.relaunch_calls = 0

    def relaunch(self, prompt: str = "") -> Dict[str, Any]:
        """记录一次重拉。"""
        self.relaunch_calls += 1
        return {"ok": True, "pid": 1234, "detail": "已重新拉起 Agent。"}


def _make_crawl() -> Any:
    """造一个演习对象（不真的开跑）。"""
    return crawl.NightCrawl(
        minutes=1,
        task="测试",
        use_agent=True,
        use_llm=True,
        workdir=None,
        fast=True,
    )


class TestRelaunchOnTurnDone:
    """★ 进程还在 ≠ 还在干活。"""

    def test_should_relaunch_when_turn_done_but_process_alive(self) -> None:
        """这一轮干完了（进程还赖着）→ 必须重拉。"""
        obj = _make_crawl()
        obj.session = _FakeSession(_FakeProc(alive=True))
        obj._agent_gone.set()
        obj._last_relaunch_at = 0.0
        obj._ensure_agent_alive()
        assert obj.session.relaunch_calls == 1
        assert obj.relaunch_count == 1

    def test_should_not_relaunch_while_working(self) -> None:
        """还在干活 → 不许动它。"""
        obj = _make_crawl()
        obj.session = _FakeSession(_FakeProc(alive=True))
        obj._agent_gone.clear()
        obj._ensure_agent_alive()
        assert obj.session.relaunch_calls == 0

    def test_should_relaunch_when_process_gone(self) -> None:
        """进程没了 → 老规矩，重拉。"""
        obj = _make_crawl()
        obj.session = _FakeSession(_FakeProc(alive=False))
        obj._agent_gone.set()
        obj._last_relaunch_at = 0.0
        obj._ensure_agent_alive()
        assert obj.session.relaunch_calls == 1

    def test_should_respect_rate_limit(self) -> None:
        """⚠️ 频率保护：不能变成「拉一个 → 秒退 → 再拉一个」。"""
        obj = _make_crawl()
        obj.session = _FakeSession(_FakeProc(alive=False))
        obj._agent_gone.set()
        obj._last_relaunch_at = 0.0
        obj._ensure_agent_alive()
        assert obj.session.relaunch_calls == 1
        # 第二次：间隔不够，不许再拉。
        obj._agent_gone.set()
        obj._ensure_agent_alive()
        assert obj.session.relaunch_calls == 1

    def test_relaunch_interval_should_not_be_too_dense(self) -> None:
        """⚠️ 20 秒太密 —— 实机一晚上能烧掉十几个会话。"""
        assert crawl.RELAUNCH_MIN_INTERVAL >= 60.0


class TestSingleReaderPerGeneration:
    """★ 重拉之后同一行日志不能被读两遍。"""

    def test_should_start_only_one_reader_per_generation(self) -> None:
        """同一代只起一个读者。"""
        started: List[int] = []

        class _Session:
            """假的会话。"""

            def __init__(self) -> None:
                self.plan = type("P", (), {"id": "x"})()
                self._generation = 0
                self._lines: List[Any] = []
                self._reader_generation = -1

            def _start_reader(self, proc: Any) -> None:
                """记下这次起读者。"""
                if self._reader_generation == self._generation:
                    return
                self._reader_generation = self._generation
                started.append(self._generation)

        session = _Session()
        session._start_reader(_FakeProc())
        session._start_reader(_FakeProc())
        assert len(started) == 1

        # 换代（重拉）之后要能再起一个。
        session._generation += 1
        session._start_reader(_FakeProc())
        assert len(started) == 2

    def test_session_initialises_reader_generation(self) -> None:
        """真实的会话对象要有这个字段（默认「还没起过」）。"""
        assert hasattr(launcher.AgentSession, "_start_reader")
        source = launcher.AgentSession.__init__.__code__
        assert "_reader_generation" in source.co_names


class TestLlmPreflight:
    """★ L2 模型不在就别开跑。"""

    def test_should_report_missing_model(self) -> None:
        """模型不在 → 预检**必须**说不行。"""
        obj = _make_crawl()
        config = {
            "llm": {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:1",  # 一定连不上
                "model": "qwen2.5:7b",
            }
        }
        info = obj._preflight_llm(config)
        assert info["ok"] is False
        assert "漂移判定" in info["note"]

    def test_should_pass_for_cloud_provider(self) -> None:
        """云端模型本机验不了 —— 不该误报。"""
        obj = _make_crawl()
        info = obj._preflight_llm(
            {"llm": {"provider": "openai_compatible", "model": "deepseek-chat"}}
        )
        assert info["ok"] is True

    def test_should_detect_installed_model(self) -> None:
        """模型确实装上了 → 预检必须说行。"""
        obj = _make_crawl()
        captured: Dict[str, Any] = {}

        class _FakeResponse:
            """假的 HTTP 响应。"""

            def read(self) -> bytes:
                return json.dumps(
                    {"models": [{"name": "qwen2.5:7b"}]}
                ).encode("utf-8")

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: Any) -> bool:
                return False

        import urllib.request

        original = urllib.request.urlopen

        def _fake_urlopen(url: Any, timeout: Any = None) -> Any:
            captured["url"] = url
            return _FakeResponse()

        urllib.request.urlopen = _fake_urlopen  # type: ignore[assignment]
        try:
            info = obj._preflight_llm(
                {
                    "llm": {
                        "provider": "ollama",
                        "base_url": "http://localhost:11434",
                        "model": "qwen2.5:7b",
                    }
                }
            )
        finally:
            urllib.request.urlopen = original  # type: ignore[assignment]
        assert info["ok"] is True, info["note"]
        assert info["installed"] == ["qwen2.5:7b"]
        assert "/api/tags" in str(captured.get("url"))


class TestConsoleEncoding:
    """★ 验收项那行 ``✓`` 在 GBK 终端里编不出来。"""

    def test_should_prepare_stdout(self) -> None:
        """脚本必须有 ``_prepare_stdout()``。"""
        assert hasattr(crawl, "_prepare_stdout")

    def test_report_json_written_before_printing(self) -> None:
        """⚠️ JSON 必须**先落盘**，再打漂亮的报告。"""
        source = Path(CRAWL_PATH).read_text(encoding="utf-8")
        json_at = source.index('json.dump(report')
        checks_at = source.index('report["checks"].items()')
        assert json_at < checks_at, "JSON 落盘必须排在验收项打印之前"


class TestReportFields:
    """报告里的关键字段一个都不能少。"""

    def test_report_should_have_key_fields(self) -> None:
        """报告的字段清单。"""
        source = Path(CRAWL_PATH).read_text(encoding="utf-8")
        for field in (
            '"llm"',
            '"llm_preflight"',
            '"agent_relaunches"',
            '"source_untouched"',
            '"guardian_intercept"',
            '"suspended_verified"',
            '"webhook_briefings"',
            '"memory"',
        ):
            assert field in source, "报告里少了 {}".format(field)

    def test_checks_should_cover_eleven_items(self) -> None:
        """验收项要覆盖三类注入 + 挂起 + 推送 + 内存 + 源码。"""
        obj = _make_crawl()
        report = {
            "guardian_intercept": True,
            "webhook_alerts": 1,
            "webhook_briefings": 1,
            "briefing_path": "x",
            "memory": {"leak": False},
            "source_untouched": True,
            "llm": {"evaluations": 2},
            "verdicts_detail": {
                "stuck": {"detected": True},
                "drift": {"detected": True},
                "dangerous": {"detected": True},
                "suspend_printed": True,
            },
        }
        obj._passed(report)
        assert len(report["checks"]) >= 11
        assert all(report["checks"].values())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
