"""v3.5.0 回归：多流隔离 · L2 极速缓存 · 记忆管理 · 级联双盲 · GUI 分区。

背景
====

v3.4.0 把「托盘化 / 卸载 / 雷达排除」这些体验坑填平了，
但内核还剩一个最疼的：**并发日志混杂**。

多个 Agent 并行时，它们往**同一个**日志文件里写，
哨兵读到的是一条**交织**的流：

.. code-block:: text

    {"type":"tool_call","thread_id":"A", ... "ls"}
    {"type":"tool_call","thread_id":"B", ... "rm -rf /"}
    {"type":"tool_call","thread_id":"A", ... "read a.py"}

共享一个滑动窗口的话，「A 读了 a.py」和「B 要删库」
被当成**同一个 Agent 的连续行为** —— 这就是「串台」。

所以 v3.5.0 做了五件事：

1. **日志流按线程指纹强制隔离**（任务一）——
   适配器给每行事件打指纹，引擎按指纹分路，看板拆成虚拟会话卡片；
2. **NInfer (L2) 极速与缓存优化**（任务二）——
   剥离时间戳 / UUID / ``[TAIL] #123``，静态前缀前置吃满 Prefix Cache，
   10 秒超时，429/503/超时一律 ``{"status":"unavailable"}``；
3. **滑动窗口与后台滚动摘要**（任务三）——
   短期 3-5 步（原子对不可拆），长期满 10 条后台异步摘要；
4. **级联模型路由 L1 → L2 → L3**（任务四）——
   L2 判正常就静默丢弃（省云端调用），L2 报警才叫 L3，
   **双盲**（两边都判有问题）才亮红灯；
5. **GUI 与发布**（任务五）——
   设置页分「L2 本地 / L3 云端」两个区，看板顶部加节省计数器。

本文件把这些**钉住**，一条都不许退化。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from src.adapters.fingerprint import (
    extract_fingerprint,
    fingerprint_label,
    fingerprint_stream_key,
    normalize_path,
)
from src.adapters.mcode import McodeAdapter
from src.core.engine import SEVERITY_RED_LINE, IntentTracker, SentinelEngine
from src.core.memory import LongTermMemory, ShortTermMemory, StreamMemory
from src.llm.cascade import (
    CASCADE_STATUS_CLEARED,
    CASCADE_STATUS_ESCALATED,
    CASCADE_STATUS_ON_TRACK,
    CASCADE_STATUS_SINGLE,
    CASCADE_STATUS_UNAVAILABLE,
    CascadeResult,
    CascadeRouter,
    CascadeVerdict,
    is_transient_reason,
)
from src.llm.normalize import (
    build_static_prefix,
    strip_volatile,
    volatile_fingerprint,
)
from src.llm.parsing import parse_cascade_payload
from src.rules.basic import RuleEngine

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "gui" / "static"
TEMPLATE_DIR = ROOT / "src" / "gui" / "templates"


def _read(*parts) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对仓库根的路径片段。
    :return: 文件文本。
    """
    path = ROOT.joinpath(*parts)
    return path.read_text(encoding="utf-8")


class _FakeProvider:
    """假模型提供方 —— 记录调用，按剧本返回。

    :param verdicts: 依次返回的判定文本。
    :param raise_with: 抛异常时用的异常对象。
    """

    def __init__(self, verdicts=None, raise_with=None, model="fake-model"):
        self.verdicts = list(verdicts or [])
        self.raise_with = raise_with
        self.model = model
        #: 每次调用记一笔（system / user）。
        self.calls = []

    def chat(self, system: str, user: str) -> str:
        """返回剧本里的下一条判定。

        :param system: 系统提示。
        :param user:   用户提示。
        :return: 判定文本。
        """
        self.calls.append((system, user))
        if self.raise_with is not None:
            raise self.raise_with
        if self.verdicts:
            return self.verdicts.pop(0)
        return '{"verdict":"ON_TRACK","reason":"正常"}'


def _event(fingerprint: str, command: str, event_type: str = "tool_call"):
    """造一个带指纹的统一事件。

    :param fingerprint: 线程指纹。
    :param command:     命令文本。
    :param event_type:  事件类型。
    :return: 事件字典。
    """
    event = {
        "source": "mcode",
        "type": event_type,
        "fingerprint": fingerprint,
        "tool": {"name": "Bash", "arguments": {}, "command": command},
        "text": "",
    }
    if event_type == "tool_result":
        event.pop("tool")
        event["text"] = command
    return event


# ===========================================================================
# 任务一：日志流按线程指纹强制隔离
# ===========================================================================
class TestFingerprintExtraction:
    """指纹提取 —— 线程 > 会话 > 工作目录 > 认不出。"""

    def test_thread_id_wins_over_session_and_cwd(self):
        payload = {
            "thread_id": "T-1",
            "session_id": "S-1",
            "cwd": "D:/work/project",
        }
        assert extract_fingerprint(payload) == "thread:T-1"

    def test_session_id_is_the_second_choice(self):
        assert extract_fingerprint({"session_id": "S-1"}) == "session:S-1"

    def test_cwd_is_the_last_resort(self):
        payload = {"cwd": "D:\\项目\\N.E.K.O"}
        # 盘符会被归一化掉（``d:/`` → 空）——
        # 指纹要的是「同一个地方」的稳定形态，不是完整路径。
        assert extract_fingerprint(payload) == "path:项目/n.e.k.o"

    def test_unknown_payload_yields_empty_string(self):
        """认不出就**不认** —— 与 v3.4.0 的行为完全一样。"""
        assert extract_fingerprint({"type": "tool_call"}) == ""

    def test_nested_payload_is_probed(self):
        payload = {"item": {"threadId": "T-9"}}
        assert extract_fingerprint(payload) == "thread:T-9"

    def test_numeric_thread_id_is_accepted(self):
        """codex 的 thread_id 是数字 —— 也得认出来。"""
        assert extract_fingerprint({"thread_id": 42}) == "thread:42"

    def test_tool_arguments_never_become_the_fingerprint(self):
        """工具参数里的文件路径**不能**当指纹 ——
        那会把同一个线程拆成碎片。
        """
        payload = {
            "type": "tool_call",
            "tool_call": {"name": "Read", "arguments": {"file_path": "a.py"}},
        }
        assert extract_fingerprint(payload) == ""

    def test_normalize_path_ignores_case_and_slashes(self):
        assert normalize_path("D:\\项目\\N.E.K.O\\") == normalize_path(
            "d:/项目/n.e.k.o"
        )

    def test_labels_are_human_readable(self):
        assert fingerprint_label("thread:abc123") == "线程 abc123"
        assert fingerprint_label("path:d:/项目/n.e.k.o") == "工作区 n.e.k.o"
        assert fingerprint_label("") == "默认流"

    def test_stream_key_falls_back_to_default(self):
        assert fingerprint_stream_key("") == "default"
        assert fingerprint_stream_key("thread:T-1") == "thread:T-1"


class TestAdapterFingerprint:
    """适配器给每一行事件打上指纹。"""

    def test_event_carries_the_fingerprint(self):
        adapter = McodeAdapter()
        line = json.dumps(
            {
                "type": "tool_call",
                "thread_id": "T-1",
                "tool_call": {"name": "Bash", "arguments": {"command": "ls"}},
            }
        )
        events = adapter.parse_line(line)
        assert len(events) == 1
        assert events[0]["fingerprint"] == "thread:T-1"

    def test_event_without_thread_has_empty_fingerprint(self):
        adapter = McodeAdapter()
        line = json.dumps(
            {
                "type": "tool_call",
                "tool_call": {"name": "Bash", "arguments": {"command": "ls"}},
            }
        )
        events = adapter.parse_line(line)
        assert events[0]["fingerprint"] == ""


class TestEngineStreamIsolation:
    """引擎按指纹分路 —— A 路的日志绝不进 B 路的滑窗。"""

    def _engine(self, tmp_path):
        """造一个带意图追踪器的引擎。

        :param tmp_path: pytest 临时目录。
        :return: ``(engine, seen)``；``seen`` 记录每次判定的窗口摘要。
        """
        seen = []

        def evaluator(goal, summary):
            seen.append((goal, summary))
            return {"drift": False, "reason": "", "summary": summary}

        tracker = IntentTracker(
            goal="重构 auth 模块",
            window=5,
            threshold=2,
            evaluator=evaluator,
        )
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine(config={}),
            alert_log_path=str(tmp_path / "sentinel_alert.log"),
            console=False,
        )
        engine.set_intent_tracker(tracker)
        return engine, seen

    def test_two_fingerprints_get_two_trackers(self, tmp_path):
        engine, _ = self._engine(tmp_path)
        try:
            engine._observe_intent(_event("thread:A", "ls"))
            engine._observe_intent(_event("thread:B", "pwd"))
            trackers = engine.stream_trackers()
            assert set(trackers) == {"thread:A", "thread:B"}
        finally:
            engine.close()

    def test_each_route_only_sees_its_own_events(self, tmp_path):
        """串台是这一版要根除的病 —— 各路的滑窗必须互不相干。"""
        engine, _ = self._engine(tmp_path)
        try:
            engine._observe_intent(_event("thread:A", "read a.py"))
            engine._observe_intent(_event("thread:B", "rm -rf build"))
            engine._observe_intent(_event("thread:A", "read b.py"))
            trackers = engine.stream_trackers()
            buffer_a = list(trackers["thread:A"]._buffer)
            buffer_b = list(trackers["thread:B"]._buffer)
            assert [item["fingerprint"] for item in buffer_a] == [
                "thread:A",
                "thread:A",
            ]
            assert [item["fingerprint"] for item in buffer_b] == ["thread:B"]
        finally:
            engine.close()

    def test_default_route_stays_the_default_tracker(self, tmp_path):
        """没有指纹的事件走默认那一路 —— 行为与 v3.4.0 一致。"""
        engine, _ = self._engine(tmp_path)
        try:
            engine._observe_intent(_event("", "ls"))
            assert engine.stream_trackers() == {}
            assert len(list(engine.intent_tracker._buffer)) == 1
        finally:
            engine.close()

    def test_switching_tracker_clears_split_trackers(self, tmp_path):
        """换装追踪器时旧的分路必须一起清掉。"""
        engine, _ = self._engine(tmp_path)
        try:
            engine._observe_intent(_event("thread:A", "ls"))
            assert engine.stream_trackers()
            engine.set_intent_tracker(None)
            assert engine.stream_trackers() == {}
        finally:
            engine.close()


# ===========================================================================
# 任务二：NInfer (L2) 极速与缓存优化
# ===========================================================================
class TestVolatileStripping:
    """动态特征剥离 —— 只动送给模型的 Prompt。"""

    def test_timestamps_are_replaced(self):
        text = "2026-10-29 23:41:05 读了文件"
        assert "2026-10-29" not in strip_volatile(text)
        assert "23:41:05" not in strip_volatile(text)

    def test_tail_line_number_is_replaced(self):
        assert "[TAIL] #123" not in strip_volatile("末尾 [TAIL] #123")

    def test_uuid_is_replaced(self):
        raw = "01a17af0-3b2c-4d5e-8f90-1234567890ab"
        assert raw not in strip_volatile(raw)

    def test_paths_and_commands_survive(self):
        """文件路径、命令、工具名**一个字都不动** ——
        那些是判定依据，剥了模型就没法判了。
        """
        text = "rm -rf D:\\项目\\N.E.K.O\\build && cat src/main.py"
        assert strip_volatile(text) == text

    def test_placeholders_are_constant(self):
        """占位符必须是**固定字符串** —— 缓存才可能命中。"""
        first = strip_volatile("2026-10-29 23:41:05")
        second = strip_volatile("2027-01-01 00:00:00")
        assert first == second

    def test_volatile_fingerprint_ignores_timestamps(self):
        left = volatile_fingerprint("2026-10-29 23:41:05 执行 ls")
        right = volatile_fingerprint("2026-10-30 00:00:00 执行 ls")
        assert left == right

    def test_volatile_fingerprint_tracks_real_differences(self):
        left = volatile_fingerprint("执行 ls")
        right = volatile_fingerprint("执行 rm -rf /")
        assert left != right


class TestStaticPrefix:
    """静态前缀 —— 同样的事实永远得到同样的字节。"""

    def test_same_facts_produce_identical_bytes(self):
        left = build_static_prefix("系统提示", goal="目标", facts={"a": "1", "b": "2"})
        right = build_static_prefix("系统提示", goal="目标", facts={"b": "2", "a": "1"})
        assert left == right

    def test_prefix_contains_goal_and_facts(self):
        text = build_static_prefix("系统提示", goal="重构 auth", facts={"档位": "ON_TRACK"})
        assert "系统提示" in text
        assert "重构 auth" in text
        assert "档位" in text

    def test_empty_facts_are_skipped(self):
        text = build_static_prefix("系统提示", facts={"a": "", "b": None})
        assert "【事实卡片】" not in text


class TestCascadePromptShape:
    """L2 的 Prompt 结构 —— 极简 + 静态前缀。"""

    def test_filter_prompt_is_minimal(self):
        from src.llm.prompts import build_filter_prompt

        prompt = build_filter_prompt(summary="第1步 [Bash] ls", action="ls")
        assert "ls" in prompt
        # 极简 —— 不该把整段历史塞进去。
        assert len(prompt) < 800

    def test_verify_prompt_differs_from_filter_prompt(self):
        """L3 的问法必须与 L2 **不一样** ——
        不然它就是照抄 L2 的结论，等于没复核。
        """
        from src.llm.prompts import build_filter_prompt, build_verify_prompt

        filter_prompt = build_filter_prompt(summary="s", action="a")
        verify_prompt = build_verify_prompt(
            summary="s",
            action="a",
            l2_verdict="DRIFT",
            l2_reason="r",
            l1_verdict="DANGEROUS",
            l1_reason="q",
        )
        assert filter_prompt != verify_prompt
        assert "DRIFT" in verify_prompt


class TestUnavailableContract:
    """429 / 503 / 超时 → ``{"status": "unavailable"}``。"""

    def test_transient_reasons_are_recognized(self):
        assert is_transient_reason("HTTP 429：触发限流（稍后重试）")
        assert is_transient_reason("HTTP 503：服务暂时不可用")
        assert is_transient_reason("Timeout: 10.0 seconds")

    def test_non_transient_reason_is_not_transient(self):
        assert not is_transient_reason("响应不是合法 JSON")

    def test_unavailable_payload_shape(self):
        verdict = CascadeVerdict(
            available=False,
            reason="HTTP 429：触发限流",
            transient=True,
            provider="l2",
        )
        payload = verdict.payload
        assert payload["status"] == "unavailable"
        assert payload["transient"] is True
        # 必须能 JSON 序列化 —— 它要写进日志行。
        assert json.dumps(payload, ensure_ascii=False)

    def test_available_payload_carries_the_verdict(self):
        verdict = CascadeVerdict(verdict="DRIFT", reason="跑偏了", provider="l2")
        payload = verdict.payload
        assert payload["status"] == "DRIFT"
        assert payload["reason"] == "跑偏了"


class TestApiClientFastPath:
    """L2 的请求体 —— 关思考 + JSON 模式 + 短超时。"""

    def test_reasoning_effort_and_json_mode_are_sent(self):
        from src.llm.api_client import OpenAICompatibleClient

        captured = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "{}"}}]}

        class _Session:
            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                captured["payload"] = json
                captured["timeout"] = timeout
                return _Resp()

        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            model="bonsai2-27b",
            timeout=10,
            timeout_floor=10,
            reasoning_effort="none",
            max_tokens=256,
            json_mode=True,
            session=_Session(),
            config={"llm": {}},
        )
        client.chat("系统提示", "用户提示")

        payload = captured["payload"]
        assert payload["reasoning_effort"] == "none"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 256

    def test_local_engine_timeout_floor_can_be_lowered(self):
        """L2 必须能把 600 秒的下限**压到 10 秒** ——
        否则永远等不满，单并发队列直接堵死。
        """
        from src.llm.api_client import OpenAICompatibleClient

        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            model="m",
            timeout=10,
            timeout_floor=10,
            session=object(),
            config={"llm": {}},
        )
        assert client.timeout == 10.0

    def test_default_local_floor_stays_at_600(self):
        """没显式压低时，27B 思考模型的下限**不能变**。"""
        from src.llm.api_client import OpenAICompatibleClient

        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            model="m",
            session=object(),
            config={"llm": {}},
        )
        assert client.timeout == 600.0


# ===========================================================================
# 任务三：滑动窗口与后台滚动摘要
# ===========================================================================
class TestShortTermMemory:
    """短期记忆 —— 原子对不可拆分。"""

    def test_window_keeps_only_the_latest_steps(self):
        memory = ShortTermMemory(capacity=2)
        for index in range(4):
            memory.observe(_event("", "cmd-{}".format(index)))
        assert memory.steps() == 2
        assert memory.events()[0]["tool"]["command"] == "cmd-2"

    def test_tool_result_joins_the_call_as_one_step(self):
        memory = ShortTermMemory(capacity=4)
        memory.observe(_event("", "ls", "tool_call"))
        memory.observe(_event("", "a.txt", "tool_result"))
        assert memory.steps() == 1
        assert len(memory.events()) == 2

    def test_eviction_takes_the_whole_pair(self):
        """挤出去的时候必须**整步一起走** ——
        只挤走一半会让模型看到「有命令没回执」。
        """
        memory = ShortTermMemory(capacity=1)
        memory.observe(_event("", "ls", "tool_call"))
        memory.observe(_event("", "a.txt", "tool_result"))
        evicted = memory.observe(_event("", "pwd", "tool_call"))
        assert len(evicted) == 2
        assert {item["type"] for item in evicted} == {"tool_call", "tool_result"}

    def test_summary_is_compact(self):
        memory = ShortTermMemory(capacity=4)
        memory.observe(_event("", "ls"))
        assert "ls" in memory.summary()


class TestLongTermMemory:
    """长期记忆 —— 后台异步摘要，失败不丢历史。"""

    def test_summary_runs_in_background_after_threshold(self):
        done = threading.Event()
        seen = {}

        def summarizer(old, events):
            seen["old"] = old
            seen["count"] = len(events)
            done.set()
            return "摘要：" + str(len(events))

        memory = LongTermMemory(summarizer, threshold=3)
        memory.push([{"type": "tool_call"}] * 3)
        assert done.wait(5.0)
        time.sleep(0.1)
        assert memory.summary.startswith("摘要：")
        assert memory.summaries == 1

    def test_failed_summary_keeps_the_history(self):
        """摘要失败**不清空**待汇总事件 —— 摘要是锦上添花。"""

        def summarizer(old, events):
            raise RuntimeError("模型挂了")

        memory = LongTermMemory(summarizer, threshold=2)
        memory.push([{"type": "tool_call"}] * 2)
        time.sleep(0.4)
        assert memory.failures >= 1
        assert memory.pending_count >= 2
        assert memory.summary == ""

    def test_no_summarizer_only_accumulates(self):
        memory = LongTermMemory(None, threshold=2)
        assert memory.push([{"type": "tool_call"}] * 5) is False
        assert memory.pending_count == 5


class TestStreamMemory:
    """一路记忆 —— 背景在前、窗口在后。"""

    def test_context_puts_background_first(self):
        memory = StreamMemory(capacity=2, threshold=100)
        memory.observe(_event("", "ls"))
        text = memory.context()
        assert text.index("【最近步骤】") >= 0

    def test_background_appears_before_the_window(self):
        memory = StreamMemory(capacity=2, threshold=1)
        memory.observe(_event("", "a"))
        memory.observe(_event("", "b"))
        memory.observe(_event("", "c"))
        time.sleep(0.4)
        text = memory.context()
        if "【历史背景】" in text:
            assert text.index("【历史背景】") < text.index("【最近步骤】")


# ===========================================================================
# 任务四：级联模型路由（L1 → L2 → L3）
# ===========================================================================
class TestCascadeRouter:
    """级联编排 —— L2 过滤、L3 复审、双盲亮红灯。"""

    def test_l2_on_track_is_silently_dropped(self):
        """L2 判正常 → **不调云端**，省下一次调用。"""
        l2 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"在读文件"}'])
        l3 = _FakeProvider([])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="ls")

        assert result.status == CASCADE_STATUS_ON_TRACK
        assert l3.calls == []
        assert router.saved_cloud_calls == 1
        assert router.l2_calls == 1
        assert router.l3_calls == 0

    def test_l2_alarm_triggers_l3(self):
        """L2 报警 → 立刻叫 L3 云端复审。"""
        l2 = _FakeProvider(['{"verdict":"DRIFT","reason":"跑偏了"}'])
        l3 = _FakeProvider(['{"verdict":"DANGEROUS","reason":"确实危险"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="rm -rf /")

        assert len(l3.calls) == 1
        assert router.l3_calls == 1
        assert result.status == CASCADE_STATUS_ESCALATED
        assert result.red_light is True
        assert router.red_lights == 1

    def test_l3_can_clear_the_l2_alarm(self):
        """L3 推翻 L2 → **不亮红灯**（宁可漏报也不要误报）。"""
        l2 = _FakeProvider(['{"verdict":"DRIFT","reason":"疑似跑偏"}'])
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"其实没问题"}'])
        router = CascadeRouter(l2, l3)

        result = router.filter(summary="s", action="ls")

        assert result.status == CASCADE_STATUS_CLEARED
        assert result.red_light is False
        assert router.red_lights == 0

    def test_single_side_never_lights_the_red_light(self):
        """只有 L2 一边有结论 → **不亮红灯**，但要记账。"""
        l2 = _FakeProvider(['{"verdict":"DRIFT","reason":"疑似"}'])
        router = CascadeRouter(l2, None)

        result = router.filter(summary="s", action="ls")

        assert result.status == CASCADE_STATUS_SINGLE
        assert result.red_light is False

    def test_l2_unavailable_falls_back_to_l1(self):
        """L2 挂了就**立刻回退 L1**，绝不死等。

        ⚠️ v3.8.0：默认行为已经改成「L2 临时性掉线 ⇒ 热切 L3」
        （单盲，不亮红灯）—— 那条路在
        :mod:`tests.test_v380_failover_and_single_instance` 里逐条覆盖。
        本用例**显式关掉热切**，用来钉住 v3.7.0 的老语义：
        没有热切时，L2 挂了就老老实实回退 L1。
        """
        l2 = _FakeProvider(raise_with=RuntimeError("HTTP 429：触发限流"))
        l3 = _FakeProvider([])
        router = CascadeRouter(l2, l3, failover_to_l3=False)

        result = router.filter(
            summary="s",
            action="ls",
            l1_verdict="DANGEROUS",
            l1_reason="命中危险命令",
        )

        assert result.status == CASCADE_STATUS_UNAVAILABLE
        assert l3.calls == []
        assert router.unavailable_calls == 1
        assert result.l2.payload["status"] == "unavailable"

    def test_breaker_opens_after_repeated_failures(self):
        """连续失败就熔断 —— 别把队列堵死。"""
        l2 = _FakeProvider(raise_with=RuntimeError("HTTP 503：服务暂时不可用"))
        l3 = _FakeProvider([])
        router = CascadeRouter(l2, l3, breaker_threshold=3, breaker_open_seconds=60)

        for _ in range(3):
            router.filter(summary="s", action="ls")

        calls_before = len(l2.calls)
        router.filter(summary="s", action="ls")

        # 熔断之后**不再调用** L2。
        assert len(l2.calls) == calls_before
        assert router.stats()["breaker_open"] is True

    def test_stats_are_counted(self):
        l2 = _FakeProvider(
            [
                '{"verdict":"ON_TRACK","reason":"正常"}',
                '{"verdict":"DRIFT","reason":"跑偏"}',
            ]
        )
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"其实没问题"}'])
        router = CascadeRouter(l2, l3)

        router.filter(summary="s", action="ls")
        router.filter(summary="s", action="pwd")

        stats = router.stats()
        assert stats["l2_calls"] == 2
        assert stats["l3_calls"] == 1
        assert stats["saved_cloud_calls"] == 1
        assert stats["red_lights"] == 0

    def test_l3_prompt_contains_the_l2_reason(self):
        """L3 必须拿到 L2 的理由与 L1 的判定 —— 否则没法复核。"""
        l2 = _FakeProvider(['{"verdict":"DRIFT","reason":"L2 的理由"}'])
        l3 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"没问题"}'])
        router = CascadeRouter(l2, l3)

        router.filter(
            summary="s",
            action="ls",
            l1_verdict="DANGEROUS",
            l1_reason="L1 的理由",
        )

        _, user_prompt = l3.calls[0]
        assert "L2 的理由" in user_prompt
        assert "DANGEROUS" in user_prompt

    def test_l2_prompt_is_stripped_and_prefixed(self):
        """L2 的 Prompt = 静态前缀 + 剥过易变内容的用户提示。"""
        l2 = _FakeProvider(['{"verdict":"ON_TRACK","reason":"正常"}'])
        router = CascadeRouter(l2, None, goal="重构 auth")

        router.filter(summary="2026-10-29 23:41:05 读了 a.py", action="ls")

        system, user_prompt = l2.calls[0]
        assert "重构 auth" in system
        assert "2026-10-29" not in user_prompt


class TestCascadePayloadParsing:
    """三值判定的解析 —— 中文 / 英文 / 脏输出都要认。"""

    def test_plain_json(self):
        parsed = parse_cascade_payload('{"verdict":"DRIFT","reason":"x"}')
        assert parsed.ok
        assert parsed.data["verdict"] == "DRIFT"

    def test_chinese_verdict(self):
        parsed = parse_cascade_payload('{"verdict":"漂移","reason":"x"}')
        assert parsed.ok
        assert parsed.data["verdict"] == "DRIFT"

    def test_labelled_fallback(self):
        parsed = parse_cascade_payload("判定: ON_TRACK\n理由: 正常")
        assert parsed.ok
        assert parsed.data["verdict"] == "ON_TRACK"

    def test_garbage_is_not_ok(self):
        parsed = parse_cascade_payload("完全不是 JSON")
        assert not parsed.ok


class TestEngineCascadeWiring:
    """引擎接线 —— 双盲命中才升级。"""

    def _engine(self, tmp_path, router):
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine(config={}),
            alert_log_path=str(tmp_path / "sentinel_alert.log"),
            console=False,
        )
        engine.set_cascade(router)
        return engine

    def test_red_light_upgrades_the_alert(self, tmp_path):
        """L2 与 L3 同时判有问题 → 升级成红线。"""
        l2 = _FakeProvider(['{"verdict":"DRIFT","reason":"跑偏"}'])
        l3 = _FakeProvider(['{"verdict":"DANGEROUS","reason":"危险"}'])
        router = CascadeRouter(l2, l3)
        engine = self._engine(tmp_path, router)
        try:
            engine.alerts.append(
                {"alert_id": "A-1", "severity": "HIGH", "verdict": "DRIFT"}
            )
            result = CascadeResult(
                l2=CascadeVerdict(verdict="DRIFT", available=True),
                l3=CascadeVerdict(verdict="DANGEROUS", available=True),
                status=CASCADE_STATUS_ESCALATED,
                red_light=True,
                reason="两边都判有问题",
            )
            engine._on_cascade_result("A-1", result)

            alert = engine.alerts[0]
            assert alert["severity"] == SEVERITY_RED_LINE
            assert alert["cascade"]["l2"] == "DRIFT"
            assert alert["cascade"]["l3"] == "DANGEROUS"
            assert alert["escalated"] is True
        finally:
            engine.close()

    def test_single_side_does_not_upgrade(self, tmp_path):
        """单边判定 → **绝不**升级。"""
        l2 = _FakeProvider(['{"verdict":"DRIFT","reason":"跑偏"}'])
        router = CascadeRouter(l2, None)
        engine = self._engine(tmp_path, router)
        try:
            engine.alerts.append(
                {"alert_id": "A-2", "severity": "HIGH", "verdict": "DRIFT"}
            )
            result = CascadeResult(
                l2=CascadeVerdict(verdict="DRIFT", available=True),
                status=CASCADE_STATUS_SINGLE,
                red_light=False,
            )
            engine._on_cascade_result("A-2", result)

            assert engine.alerts[0]["severity"] == "HIGH"
            assert "cascade" not in engine.alerts[0]
        finally:
            engine.close()

    def test_cascade_is_off_by_default(self, tmp_path):
        """没装级联时行为与 v3.4.0 **一字不差**。"""
        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine(config={}),
            alert_log_path=str(tmp_path / "sentinel_alert.log"),
            console=False,
        )
        try:
            assert engine.cascade is None
            engine.alerts.append({"alert_id": "A-3", "severity": "HIGH"})
            engine._on_cascade_result(
                "A-3",
                CascadeResult(
                    l2=CascadeVerdict(verdict="DRIFT"),
                    l3=CascadeVerdict(verdict="DANGEROUS"),
                    status=CASCADE_STATUS_ESCALATED,
                    red_light=True,
                ),
            )
            # 没有级联就没有双盲 —— 不该凭空升级。
            assert engine.alerts[0]["severity"] == "HIGH"
        finally:
            engine.close()


# ===========================================================================
# 任务五：GUI（虚拟会话卡片 · 节省计数器 · 设置页分区）
# ===========================================================================
class TestGuiStreams:
    """看板按指纹把一份日志拆成多路。"""

    def test_store_groups_alerts_by_fingerprint(self):
        from src.core.session_server import AlertStore

        store = AlertStore()
        store.add_alert({"alert_id": "1", "fingerprint": "thread:A"})
        store.add_alert({"alert_id": "2", "fingerprint": "thread:A"})
        store.add_alert({"alert_id": "3", "fingerprint": "thread:B"})

        streams = {item["key"]: item for item in store.streams()}
        assert set(streams) == {"thread:A", "thread:B"}
        assert streams["thread:A"]["alerts"] == 2
        assert streams["thread:B"]["alerts"] == 1

    def test_store_groups_ontrack_entries(self):
        from src.core.session_server import AlertStore

        store = AlertStore()
        store.add_ontrack(
            {"summary": "正常读取", "fingerprint": "thread:A", "timestamp": 1.0}
        )
        streams = {item["key"]: item for item in store.streams()}
        assert streams["thread:A"]["ontrack"] == 1

    def test_unlabelled_entries_go_to_the_default_route(self):
        from src.core.session_server import AlertStore

        store = AlertStore()
        store.add_alert({"alert_id": "1"})
        streams = store.streams()
        assert streams[0]["key"] == "default"

    def test_snapshot_carries_streams_and_cascade(self):
        from src.core.session_server import AlertStore

        store = AlertStore()
        store.set_cascade_stats({"saved_cloud_calls": 7, "l2_calls": 9})
        snapshot = store.snapshot("watch.log", "alert.log")
        assert snapshot["cascade"]["saved_cloud_calls"] == 7
        assert "streams" in snapshot


class TestGuiCascadeLine:
    """级联统计行 —— 与引擎是一对日志格式契约。"""

    def test_parse_cascade_line(self):
        from src.core.session_server import parse_cascade_line

        line = 'CASCADE_JSON {"l2_calls": 3, "saved_cloud_calls": 2}'
        data = parse_cascade_line(line)
        assert data is not None
        assert data["saved_cloud_calls"] == 2

    def test_parse_cascade_line_ignores_other_lines(self):
        from src.core.session_server import parse_cascade_line

        assert parse_cascade_line("普通人读的一行") is None
        assert parse_cascade_line("CASCADE_JSON 不是 JSON") is None

    def test_engine_and_gui_share_the_same_marker(self):
        from src.core.engine import CASCADE_MARKER as ENGINE_MARKER
        from src.core.session_server import CASCADE_MARKER as GUI_MARKER

        assert ENGINE_MARKER == GUI_MARKER == "CASCADE_JSON"


class TestGuiConfigPlumbing:
    """设置页 —— L2 / L3 两个区都要能存回配置。"""

    def test_collect_config_updates_maps_cascade_paths(self):
        from src.core.session_server import _collect_config_updates

        updates = _collect_config_updates(
            {
                "cascade": {
                    "enabled": True,
                    "require_agreement": True,
                    "breaker_threshold": 3,
                    "l2": {
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:8888/v1",
                        "model": "bonsai2-27b",
                        "timeout": 10,
                        "timeout_floor": 10,
                        "max_tokens": 256,
                        "reasoning_effort": "none",
                    },
                    "l3": {
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "model": "strong-model",
                        "api_key": "sk-secret",
                    },
                }
            }
        )
        paths = dict(updates)
        assert paths["llm.cascade.enabled"] is True
        assert paths["llm.cascade.require_agreement"] is True
        assert paths["llm.cascade.breaker_threshold"] == 3
        assert paths["llm.cascade.l2.timeout"] == 10
        assert paths["llm.cascade.l2.timeout_floor"] == 10
        assert paths["llm.cascade.l2.max_tokens"] == 256
        assert paths["llm.cascade.l2.reasoning_effort"] == "none"
        assert paths["llm.cascade.l3.model"] == "strong-model"
        assert paths["llm.cascade.l3.api_key"] == "sk-secret"

    def test_masked_secret_is_never_written_back(self):
        """打码值回传时**必须跳过** —— 否则真密钥会被抹掉。"""
        from src.core.session_server import (
            SECRET_MASK,
            _collect_config_updates,
        )

        updates = _collect_config_updates(
            {"cascade": {"l3": {"api_key": SECRET_MASK}}}
        )
        assert updates == []


class TestFrontendAssets:
    """前端 —— 看板与设置页必须真的把新东西露出来。"""







class TestExampleConfig:
    """示例配置 —— 级联那一段必须能解析。"""

    def test_example_config_has_the_cascade_block(self):
        import yaml

        data = yaml.safe_load(
            _read("config", "config.example.yaml").replace(
                "${", "{"
            ).replace("}", "}")
        )
        cascade = (data.get("llm") or {}).get("cascade") or {}
        assert cascade.get("enabled") is False
        assert cascade.get("require_agreement") is True
        assert (cascade.get("l2") or {}).get("timeout") == 10
        assert (cascade.get("l2") or {}).get("reasoning_effort") == "none"
        assert (cascade.get("l3") or {}).get("provider") == "openai_compatible"
