"""意图漂移检测测试。

覆盖三层：

1. :func:`src.core.engine.build_window_summary` —— 摘要长什么样；
2. :class:`src.core.engine.IntentTracker` —— 滑窗与「连续 2 次才告警」；
3. 引擎侧 —— 漂移告警确实产生了，而且**没有**触发守护者挂起。

L2 全部用假客户端（Mock），**不联网**。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.core.engine import (
    DRIFT_ALERT_REASON,
    VERDICT_DRIFT,
    IntentTracker,
    SentinelEngine,
    build_window_summary,
)
from src.llm.base import DriftResult
from src.llm.parsing import parse_drift_payload


# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------
class FakeEvaluator:
    """假判定器：按预设顺序返回漂移结论。"""

    def __init__(self, results: List[DriftResult]) -> None:
        self.results = list(results)
        self.calls: List[tuple] = []

    def __call__(self, goal: str, summary: str) -> DriftResult:
        self.calls.append((goal, summary))
        if self.results:
            return self.results.pop(0)
        return DriftResult(drift=False, reason="")


def _drift(reason: str = "第3步起在改数据库连接池，与目标无关") -> DriftResult:
    """一个「判定为漂移」的结果。"""
    return DriftResult(drift=True, reason=reason)


def _no_drift(reason: str = "仍在推进目标") -> DriftResult:
    """一个「没有漂移」的结果。"""
    return DriftResult(drift=False, reason=reason)


def _degraded(reason: str = "模型不可用") -> DriftResult:
    """一个「降级」的结果（**不算漂移**）。"""
    return DriftResult(drift=False, reason="", degraded=True, detail=reason)


def _thinking(text: str) -> Dict[str, Any]:
    """构造一个 thinking 事件。"""
    return {
        "source": "mcode",
        "type": "thinking",
        "tool": None,
        "text": text,
        "is_error": False,
    }


def _tool(name: str, command: str = "") -> Dict[str, Any]:
    """构造一个 tool_call 事件。"""
    return {
        "source": "mcode",
        "type": "tool_call",
        "tool": {
            "name": name,
            "arguments": {"command": command} if command else {},
            "command": command,
        },
        "text": "",
        "is_error": False,
    }


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------
class TestWindowSummary:
    """窗口摘要的构造。"""

    def test_should_render_thinking_and_tool_calls(self) -> None:
        """每一步都要带上序号、类型与内容。"""
        summary = build_window_summary([
            _thinking("用户让我清理构建产物"),
            _tool("Bash", "rm -rf /tmp/build"),
            _tool("Read", "D:/proj/package.json"),
        ])
        assert "第1步 [思考] 用户让我清理构建产物" in summary
        assert "第2步 [Bash] rm -rf /tmp/build" in summary
        assert "第3步 [Read] D:/proj/package.json" in summary

    def test_should_fall_back_to_file_path_for_read_tools(self) -> None:
        """Read / Edit 没有命令文本 —— 要退到文件路径，否则摘要全是废话。

        文件路径恰恰是判断漂移最关键的信号：
        「一直在读同一个文件」= 在推进目标，「突然去读无关目录」= 跑偏了。
        """
        summary = build_window_summary([
            {
                "type": "tool_call",
                "tool": {
                    "name": "Read",
                    "arguments": {"file_path": "src/auth/login.py"},
                    "command": "",
                },
            },
        ])
        assert "第1步 [Read] file_path=src/auth/login.py" in summary
        assert "无命令文本" not in summary

    def test_should_prefer_command_over_arguments(self) -> None:
        """有命令文本时优先用命令 —— 命令比路径更能说明这一步在干什么。"""
        summary = build_window_summary([
            {
                "type": "tool_call",
                "tool": {
                    "name": "Bash",
                    "arguments": {
                        "command": "pytest tests/test_auth.py -q",
                        "file_path": "src/auth/login.py",
                    },
                    "command": "pytest tests/test_auth.py -q",
                },
            },
        ])
        assert "第1步 [Bash] pytest tests/test_auth.py -q" in summary
        assert "file_path=" not in summary

    def test_should_truncate_long_steps(self) -> None:
        """单步必须硬截断，否则 5 步的上下文会失控。"""
        summary = build_window_summary([_thinking("很长的思考" * 200)])
        assert len(summary) < 400
        assert "…" in summary

    def test_should_flatten_whitespace(self) -> None:
        """换行与多余空白要压掉 —— 摘要给模型看，排版是噪声。"""
        summary = build_window_summary([_thinking("第一行\n\n  第二行   第三行")])
        assert "\n" not in summary
        assert "第一行 第二行 第三行" in summary

    def test_should_ignore_non_window_events(self) -> None:
        """text / tool_result 不参与摘要。"""
        summary = build_window_summary([
            _tool("Bash", "ls"),
            {"type": "text", "text": "这是结果文本"},
            {"type": "tool_result", "text": "输出"},
        ])
        assert "这是结果文本" not in summary
        assert "输出" not in summary
        assert "第1步" in summary


# ---------------------------------------------------------------------------
# 滑窗
# ---------------------------------------------------------------------------
class TestIntentTracker:
    """``IntentTracker`` 的行为。"""

    def test_should_not_evaluate_before_window_is_full(self) -> None:
        """窗口没满不做判定 —— 不能拿 1 步就判漂移。"""
        evaluator = FakeEvaluator([_drift()])
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)
        for index in range(4):
            assert tracker.observe(_tool("Bash", f"step{index}")) is None
        assert evaluator.calls == []

    def test_should_evaluate_when_window_is_full(self) -> None:
        """窗口满了就判定一次。"""
        evaluator = FakeEvaluator([_no_drift()])
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)
        for index in range(4):
            tracker.observe(_tool("Bash", f"step{index}"))
        result = tracker.observe(_tool("Bash", "step4"))
        assert result is not None
        assert result["drift"] is False
        assert result["steps"] == 5
        assert len(evaluator.calls) == 1

    def test_should_alert_after_two_consecutive_drifts(self) -> None:
        """连续 2 次判定为漂移（= 10 步）才告警。"""
        evaluator = FakeEvaluator([_drift(), _drift()])
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)

        # 第一个窗口：漂移，但还没到阈值
        for index in range(5):
            result = tracker.observe(_tool("Bash", f"a{index}"))
        assert result is not None
        assert result["drift"] is True
        assert result["alert"] is False
        assert tracker.consecutive_drift == 1

        # 第二个窗口：连续第二次漂移 → 告警
        for index in range(5):
            result = tracker.observe(_tool("Bash", f"b{index}"))
        assert result is not None
        assert result["drift"] is True
        assert result["alert"] is True

    def test_should_reset_counter_when_not_drifting(self) -> None:
        """中间出现一次「没漂移」，连续计数要归零。"""
        evaluator = FakeEvaluator([_drift(), _no_drift(), _drift()])
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)

        for index in range(5):
            tracker.observe(_tool("Bash", f"a{index}"))
        assert tracker.consecutive_drift == 1

        for index in range(5):
            tracker.observe(_tool("Bash", f"b{index}"))
        assert tracker.consecutive_drift == 0

        for index in range(5):
            result = tracker.observe(_tool("Bash", f"c{index}"))
        assert result is not None
        assert result["alert"] is False, "计数归零后不该再告警"

    def test_degraded_should_not_count_as_drift(self) -> None:
        """模型降级**不算漂移**，而且连续计数归零。"""
        evaluator = FakeEvaluator([_drift(), _degraded()])
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)

        for index in range(5):
            tracker.observe(_tool("Bash", f"a{index}"))
        assert tracker.consecutive_drift == 1

        for index in range(5):
            result = tracker.observe(_tool("Bash", f"b{index}"))
        assert result is not None
        assert result["degraded"] is True
        assert result["drift"] is False
        assert tracker.consecutive_drift == 0

    def test_should_do_nothing_without_goal(self) -> None:
        """没有目标就没有「偏离」可言 —— 不判定、不打扰模型。"""
        evaluator = FakeEvaluator([_drift()])
        tracker = IntentTracker(goal="", evaluator=evaluator)
        for index in range(10):
            assert tracker.observe(_tool("Bash", f"step{index}")) is None
        assert evaluator.calls == []

    def test_should_do_nothing_when_disabled(self) -> None:
        """关闭时完全不工作。"""
        evaluator = FakeEvaluator([_drift()])
        tracker = IntentTracker(
            goal="重构 auth 模块", enabled=False, evaluator=evaluator
        )
        for index in range(10):
            assert tracker.observe(_tool("Bash", f"step{index}")) is None
        assert evaluator.calls == []

    def test_should_pass_goal_and_summary_to_evaluator(self) -> None:
        """判定器要拿到「目标 + 摘要」两样东西。

        ⚠️ v3.9.0：这一步的窗口里**刻意不放** ``pytest`` ——
        标准验证命令现在会被**确定性白名单**直接放行
        （根本不发 L2 请求），窗口也就不会交给判定器了。
        白名单自己的用例见 ``tests/test_v390_release.py``。
        """
        evaluator = FakeEvaluator([_no_drift()])
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)
        events = [
            _thinking("先看看目录"),
            _tool("Bash", "ls -la"),
            _tool("Read", "D:/proj/auth.py"),
            _tool("Edit", "D:/proj/auth.py"),
            _tool("Bash", "git status --short"),
        ]
        for event in events:
            tracker.observe(event)

        goal, summary = evaluator.calls[0]
        assert goal == "重构 auth 模块"
        # ⚠️ v4.0.0：摘要格式换成「演进流」——
        #    旧格式「第N步 [类型] 内容」→ 新格式「[N] [类型] 内容 (状态)」。
        #    这是**有意的**输入契约变更（v4.0.0 版本文档「已知边界」有记）。
        assert "[1] [Bash] ls -la" in summary
        assert "[4] [Bash] git status --short" in summary
        # 「先看看目录」没命中压缩器的意图动词表，被整条丢掉 ——
        # 引擎的兜底补充块把它捞了回来（L2 判定不能丢这句话）。
        assert "· 先看看目录" in summary

    def test_set_goal_should_reset_counters(self) -> None:
        """运行时换目标（看板输入框）要清零连续计数。"""
        evaluator = FakeEvaluator([_drift()])
        tracker = IntentTracker(goal="旧目标", evaluator=evaluator)
        for index in range(5):
            tracker.observe(_tool("Bash", f"a{index}"))
        assert tracker.consecutive_drift == 1

        tracker.set_goal("新目标")
        assert tracker.goal == "新目标"
        assert tracker.consecutive_drift == 0

    def test_evaluator_exception_should_not_break_tracker(self) -> None:
        """判定器抛异常不能让追踪器炸掉。"""

        def broken(goal: str, summary: str) -> DriftResult:
            raise RuntimeError("模型炸了")

        tracker = IntentTracker(goal="重构 auth 模块", evaluator=broken)
        for index in range(5):
            assert tracker.observe(_tool("Bash", f"step{index}")) is None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
class TestDriftParsing:
    """漂移判定的容错解析。"""

    def test_should_parse_plain_json(self) -> None:
        """标准 JSON。"""
        parsed = parse_drift_payload(
            '{"drift": true, "reason": "第3步起跑偏了"}'
        )
        assert parsed.ok is True
        assert parsed.data["drift"] is True
        assert parsed.data["reason"] == "第3步起跑偏了"

    def test_should_parse_json_in_code_fence(self) -> None:
        """代码围栏要能剥掉。"""
        parsed = parse_drift_payload(
            '```json\n{"drift": false, "reason": "还在推进"}\n```'
        )
        assert parsed.ok is True
        assert parsed.data["drift"] is False

    def test_should_parse_string_boolean(self) -> None:
        """模型给了字符串布尔也要认。"""
        parsed = parse_drift_payload('{"drift": "true", "reason": "x"}')
        assert parsed.ok is True
        assert parsed.data["drift"] is True

    def test_should_parse_chinese_boolean(self) -> None:
        """中文的「是 / 否」也要认。"""
        parsed = parse_drift_payload('{"drift": "是", "reason": "跑偏了"}')
        assert parsed.ok is True
        assert parsed.data["drift"] is True

    def test_should_parse_labelled_text(self) -> None:
        """标注式文本兜底。"""
        parsed = parse_drift_payload(
            "drift: true\nreason: 改起了无关的模块"
        )
        assert parsed.ok is True
        assert parsed.data["drift"] is True
        assert parsed.data["reason"] == "改起了无关的模块"

    def test_should_fail_on_garbage(self) -> None:
        """完全认不出来就失败 —— 交由调用方降级。"""
        parsed = parse_drift_payload("我不知道")
        assert parsed.ok is False

    def test_should_fail_when_drift_missing(self) -> None:
        """没有 drift 字段就是失败（不能默认当成没漂移就算数）。"""
        parsed = parse_drift_payload('{"reason": "随便说说"}')
        assert parsed.ok is False


# ---------------------------------------------------------------------------
# L2 提供方
# ---------------------------------------------------------------------------
class TestProviderEvaluateDrift:
    """``LLMProvider.evaluate_drift``。"""

    def test_should_return_drift(self) -> None:
        """正常判定。"""
        from src.llm.ollama_client import OllamaClient

        class _Session:
            def post(self, url, json=None, timeout=None):
                class _Resp:
                    status_code = 200

                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {
                            "message": {
                                "content": '{"drift": true, "reason": "跑偏"}'
                            }
                        }

                return _Resp()

        client = OllamaClient(
            session=_Session(),
            config={
                "llm": {
                    "enabled": True,
                    "base_url": "http://127.0.0.1:11434",
                    "model": "qwen2.5:7b",
                }
            },
        )
        result = client.evaluate_drift("重构 auth 模块", "第1步 [思考] …")
        assert result.degraded is False
        assert result.drift is True
        assert result.reason == "跑偏"

    def test_should_degrade_without_goal(self) -> None:
        """没目标就不该去打扰模型。"""
        from src.llm.ollama_client import OllamaClient

        class _Session:
            def post(self, *args, **kwargs):
                raise AssertionError("没有目标时不该发请求")

        client = OllamaClient(
            session=_Session(),
            config={
                "llm": {
                    "enabled": True,
                    "base_url": "http://127.0.0.1:11434",
                    "model": "qwen2.5:7b",
                }
            },
        )
        result = client.evaluate_drift("", "第1步 [思考] …")
        assert result.degraded is True
        assert result.drift is False


# ---------------------------------------------------------------------------
# 引擎集成
# ---------------------------------------------------------------------------
class TestEngineDrift:
    """引擎侧的漂移告警。"""

    def _run_engine(
        self,
        tmp_path: Any,
        evaluator: Any,
        events: List[Dict[str, Any]],
        guardian: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """跑一遍引擎，返回摘要。"""
        from src.adapters.mcode import McodeAdapter
        from src.rules.basic import RuleEngine

        log_path = tmp_path / "agent.jsonl"
        lines = []
        for event in events:
            if event["type"] == "thinking":
                lines.append(json.dumps(
                    {"type": "thinking", "thinking": event["text"]},
                    ensure_ascii=False,
                ))
            else:
                lines.append(json.dumps(
                    {
                        "type": "tool_call",
                        "tool_call": {
                            "name": event["tool"]["name"],
                            "arguments": {
                                "command": event["tool"]["command"]
                            },
                        },
                    },
                    ensure_ascii=False,
                ))
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tracker = IntentTracker(
            goal="重构 auth 模块并修复登录 bug", evaluator=evaluator
        )
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine({"rules": {"enabled": True}}),
            alert_log_path=str(tmp_path / "alert.log"),
            console=False,
            intent_tracker=tracker,
            guardian=guardian,
        )
        try:
            return engine.run(str(log_path))
        finally:
            engine.close()

    def test_should_emit_drift_alert(self, tmp_path: Any) -> None:
        """连续两次漂移要产生一条 DRIFT 告警。"""
        evaluator = FakeEvaluator([_drift(), _drift()])
        events = [_tool("Bash", f"step{i}") for i in range(10)]
        summary = self._run_engine(tmp_path, evaluator, events)

        drift_alerts = [
            alert
            for alert in summary["alerts"]
            if alert["verdict"] == VERDICT_DRIFT
        ]
        assert len(drift_alerts) == 1
        alert = drift_alerts[0]
        assert alert["reason"] == DRIFT_ALERT_REASON
        assert alert["level"] == "warn"
        assert alert["rule_id"] == "intent_drift"

    def test_drift_alert_must_not_trigger_guardian(self, tmp_path: Any) -> None:
        """漂移**不是红线** —— 守护者不能挂起进程。"""
        from src.core.guardian import Guardian

        class _Controller:
            prefer = "auto"

            def __init__(self):
                self.calls = []

            def intervene(self, pid, action=""):
                self.calls.append((pid, action))
                return {
                    "ok": True, "action": "suspend", "pid": pid,
                    "detail": "", "error": "",
                }

            def resume(self, pid):
                return {
                    "ok": True, "action": "resume", "pid": pid,
                    "detail": "", "error": "",
                }

        controller = _Controller()
        guardian = Guardian(
            {
                "alert": {"intervention_mode": "guardian"},
                "guardian": {"enabled": True, "target_pid": 4242},
            },
            controller=controller,
        )
        evaluator = FakeEvaluator([_drift(), _drift()])
        events = [_tool("Bash", f"step{i}") for i in range(10)]
        summary = self._run_engine(
            tmp_path, evaluator, events, guardian=guardian
        )

        drift_alerts = [
            alert
            for alert in summary["alerts"]
            if alert["verdict"] == VERDICT_DRIFT
        ]
        assert drift_alerts, "应该产生漂移告警"
        assert drift_alerts[0]["severity"] != "red_line"
        assert controller.calls == [], "漂移告警不该触发守护者干预"
