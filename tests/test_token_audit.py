"""Token 消耗审计测试 —— v1.1.0。

覆盖：

1. **估算规则** —— CJK 与非 CJK 的字符/token 换算；
2. **usage 提取** —— codex / OpenAI 兼容 / 平铺三种形态；
3. **台账** —— 累计、快照、每步均摊；
4. **「挽回 Token」估算** —— 公式与文案；
5. **引擎接线** —— 告警带 ``token_usage``，拦截/漂移带 ``token_saved``，
   告警日志写出 ``TOKEN_JSON`` 账单行；
6. **看板接线** —— ``TOKEN_JSON`` 解析、台账存取、快照透出。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from src.core.engine import TOKEN_MARKER, SentinelEngine
from src.core.session_server import (
    AlertStore,
    normalize_alert,
    parse_token_line,
)
from src.core.tokens import (
    DEFAULT_REMAINING_STEPS,
    TokenLedger,
    estimate_tokens,
    extract_usage,
    format_token_count,
)


# ---------------------------------------------------------------------------
# 估算规则
# ---------------------------------------------------------------------------
class TestEstimateTokens:
    """字符 → Token 的估算。"""

    def test_empty_text_is_zero(self) -> None:
        """空文本不产生 Token。"""
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0  # type: ignore[arg-type]

    def test_ascii_is_four_chars_per_token(self) -> None:
        """英文按 4 字符 ≈ 1 Token。"""
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 400) == 100

    def test_cjk_is_one_token_per_char(self) -> None:
        """中文按 1 字 ≈ 1 Token（保守）。"""
        assert estimate_tokens("中文") == 2
        assert estimate_tokens("哨兵在审计") == 5

    def test_mixed_text_sums_both(self) -> None:
        """中英混排要分开算。"""
        # 2 个中文字 + 8 个 ASCII（8/4=2）= 4
        assert estimate_tokens("中文abcdefgh") == 4


class TestFormatTokenCount:
    """Token 数的文案。"""

    def test_small_number_is_plain(self) -> None:
        """不到 1 万直接给数字。"""
        assert format_token_count(842) == "842 Token"

    def test_large_number_uses_wan(self) -> None:
        """≥ 1 万用「万」做单位。"""
        assert format_token_count(32000) == "3.2 万 Token"

    def test_negative_is_treated_as_zero(self) -> None:
        """负数不能变成负的 Token。"""
        assert format_token_count(-5) == "0 Token"


# ---------------------------------------------------------------------------
# usage 提取
# ---------------------------------------------------------------------------
class TestExtractUsage:
    """从各种形态的载荷里挖出 usage。"""

    def test_codex_turn_completed(self) -> None:
        """codex 的 ``turn.completed`` 形态。"""
        usage = extract_usage(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 73546,
                    "cached_input_tokens": 48768,
                    "output_tokens": 120,
                },
            }
        )
        assert usage is not None
        assert usage["input_tokens"] == 73546
        assert usage["output_tokens"] == 120
        assert usage["cached_input_tokens"] == 48768

    def test_openai_compatible_shape(self) -> None:
        """OpenAI 兼容 API 的 ``prompt_tokens`` / ``completion_tokens``。"""
        usage = extract_usage(
            {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        )
        assert usage is not None
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 20

    def test_flat_usage_on_event_root(self) -> None:
        """usage 被平铺在事件根上也能认。"""
        usage = extract_usage({"input_tokens": 42, "output_tokens": 7})
        assert usage is not None
        assert usage["input_tokens"] == 42

    def test_no_usage_returns_none(self) -> None:
        """没有 usage 就返回 ``None``（不瞎猜）。"""
        assert extract_usage({"type": "item.completed"}) is None
        assert extract_usage({"usage": {}}) is None
        assert extract_usage("不是字典") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------
class TestTokenLedger:
    """一次会话的 Token 台账。"""

    def test_text_events_accumulate(self) -> None:
        """估算值要累计起来。"""
        ledger = TokenLedger()
        ledger.record_event(text="a" * 400)
        ledger.record_event(text="b" * 400)
        snap = ledger.snapshot()
        assert snap["total_tokens"] == 200
        assert snap["events"] == 2
        assert snap["estimated"] is True

    def test_exact_usage_is_preferred(self) -> None:
        """有 usage 就用真值，不再叠加估算。"""
        ledger = TokenLedger()
        ledger.record_event(text="x" * 4000)
        ledger.record_event(usage={"input_tokens": 500, "output_tokens": 50})
        snap = ledger.snapshot()
        assert snap["input_tokens"] == 500
        assert snap["output_tokens"] == 50
        assert snap["total_tokens"] == 550

    def test_total_tokens_field_wins(self) -> None:
        """上游给了总数就以它为准，避免重复累加分项。"""
        ledger = TokenLedger()
        ledger.record_event(
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 999}
        )
        assert ledger.total_tokens == 999

    def test_avg_per_event(self) -> None:
        """每步均摊要算对。"""
        ledger = TokenLedger()
        ledger.record_event(text="a" * 400)
        ledger.record_event(text="b" * 400)
        assert ledger.avg_tokens_per_event() == 100.0

    def test_empty_ledger_has_zero_average(self) -> None:
        """空台账不能除零。"""
        assert TokenLedger().avg_tokens_per_event() == 0.0


class TestEstimateSaved:
    """「挽回 Token」的估算。"""

    def test_formula(self) -> None:
        """挽回 = 已烧掉的 + 每步均摊 × 预估剩余步数。"""
        ledger = TokenLedger(remaining_steps=10)
        ledger.record_event(text="a" * 400)   # 100 Token
        ledger.record_event(text="b" * 400)   # 100 Token
        saved = ledger.estimate_saved()
        # 已烧掉 200；每步均摊 100；还要跑 10 步 = 1000
        assert saved["burned"] == 200
        assert saved["upcoming"] == 1000
        assert saved["tokens"] == 1200
        assert saved["steps"] == 10

    def test_text_is_human_readable(self) -> None:
        """文案要能直接给人看。"""
        ledger = TokenLedger()
        ledger.record_event(text="a" * 40000)
        saved = ledger.estimate_saved()
        assert "预估为您挽回" in saved["text"]
        assert "Token" in saved["text"]

    def test_default_remaining_steps(self) -> None:
        """默认预估 10 步。"""
        assert DEFAULT_REMAINING_STEPS == 10
        assert TokenLedger().remaining_steps == 10


# ---------------------------------------------------------------------------
# 引擎接线
# ---------------------------------------------------------------------------
class _StubGuardian:
    """假守护者 —— 只记录被叫了几次。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: List[Dict[str, Any]] = []

    def intervene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """记录一次干预请求。"""
        self.calls.append(payload)
        return {
            "ok": self.ok,
            "message": "已挂起目标进程" if self.ok else "干预失败",
            "pid": 1234,
        }


def _engine(tmp_path: Any, guardian: Any = None) -> SentinelEngine:
    """造一个最小可用的引擎。"""
    from src.adapters.mcode import McodeAdapter
    from src.rules.basic import RuleEngine

    return SentinelEngine(
        McodeAdapter(),
        RuleEngine(None),
        alert_log_path=str(tmp_path / "alerts.log"),
        console=False,
        guardian=guardian,
        agent_name="codex",
    )


def _red_line_event(command: str) -> Dict[str, Any]:
    """造一条会命中红线的 mcode 事件。"""
    return {
        "source": "mcode",
        "type": "tool_call",
        "session_id": None,
        "timestamp": None,
        "tool": {"name": "Bash", "command": command, "arguments": {}},
        "text": "",
        "is_error": False,
        "raw": {},
    }


class TestEngineTokenWiring:
    """引擎侧的 Token 接线。"""

    def test_alert_carries_token_usage(self, tmp_path: Any) -> None:
        """告警里要带上「到现在为止烧了多少 Token」。"""
        engine = _engine(tmp_path)
        try:
            engine.process_event(_red_line_event("rm -rf /"))
            assert engine.alerts
            usage = engine.alerts[0].get("token_usage")
            assert isinstance(usage, dict)
            assert "total_tokens" in usage
        finally:
            engine.close()

    def test_guardian_success_attaches_token_saved(self, tmp_path: Any) -> None:
        """★ 真的拦下来了 → 才给「挽回 Token」。"""
        guardian = _StubGuardian(ok=True)
        engine = _engine(tmp_path, guardian)
        try:
            engine.process_event(_red_line_event("rm -rf /"))
            alert = engine.alerts[0]
            assert alert.get("token_saved"), "拦下来了就该有挽回估算"
            assert "预估为您挽回" in str(alert.get("token_saved_text"))
        finally:
            engine.close()

    def test_guardian_failure_has_no_token_saved(self, tmp_path: Any) -> None:
        """★ 拦不住 → **不**给「挽回」—— 那是句空话。"""
        guardian = _StubGuardian(ok=False)
        engine = _engine(tmp_path, guardian)
        try:
            engine.process_event(_red_line_event("rm -rf /"))
            alert = engine.alerts[0]
            assert not alert.get("token_saved")
        finally:
            engine.close()

    def test_token_line_is_written_to_alert_log(self, tmp_path: Any) -> None:
        """★ 告警日志里要写出 ``TOKEN_JSON`` 账单行（看板靠它刷新计数器）。"""
        path = tmp_path / "alerts.log"
        engine = SentinelEngine(
            _adapter(), _rules(), alert_log_path=str(path), console=False
        )
        try:
            for _ in range(12):
                engine.process_event(
                    {
                        "source": "mcode",
                        "type": "thinking",
                        "session_id": None,
                        "timestamp": None,
                        "tool": None,
                        "text": "一段比较长的思考内容，用来把估算的 Token 数推过节流阈值。" * 4,
                        "is_error": False,
                        "raw": {},
                    }
                )
            engine.flush_tokens()
        finally:
            engine.close()
        text = path.read_text(encoding="utf-8")
        assert TOKEN_MARKER in text
        line = [
            row
            for row in text.splitlines()
            if TOKEN_MARKER in row
        ][-1]
        payload = json.loads(line[line.find(TOKEN_MARKER) + len(TOKEN_MARKER):])
        assert payload["total_tokens"] > 0


def _adapter() -> Any:
    """造一个 mcode 适配器。"""
    from src.adapters.mcode import McodeAdapter

    return McodeAdapter()


def _rules() -> Any:
    """造一个规则引擎。"""
    from src.rules.basic import RuleEngine

    return RuleEngine(None)


# ---------------------------------------------------------------------------
# 看板接线
# ---------------------------------------------------------------------------
class TestDashboardTokenWiring:
    """看板侧的 Token 接线。"""

    def test_parse_token_line(self) -> None:
        """``TOKEN_JSON`` 行要被认出来。"""
        payload = {"total_tokens": 1234, "events": 10, "estimated": False}
        line = f"2026-10-27 10:00:00,123 WARNING {TOKEN_MARKER} {json.dumps(payload)}"
        parsed = parse_token_line(line)
        assert parsed is not None
        assert parsed["total_tokens"] == 1234

    def test_alert_line_is_not_a_token_line(self) -> None:
        """普通告警行不能被当成账单行。"""
        line = '2026-10-27 10:00:00,123 WARNING ALERT_JSON {"verdict": "DANGEROUS"}'
        assert parse_token_line(line) is None

    def test_store_keeps_tokens(self) -> None:
        """台账存取要正确。"""
        store = AlertStore()
        store.set_tokens({"total_tokens": 500, "events": 3, "estimated": True})
        tokens = store.tokens()
        assert tokens["total_tokens"] == 500
        assert tokens["estimated"] is True
        assert store.status()["tokens"]["total_tokens"] == 500

    def test_store_ignores_bad_payload(self) -> None:
        """坏数据不能把看板搞崩。"""
        store = AlertStore()
        store.set_tokens("不是字典")  # type: ignore[arg-type]
        assert store.tokens() == {}

    def test_snapshot_carries_tokens(self) -> None:
        """快照必须带 Token —— 前端切会话时走的就是这个接口。

        ⚠️ 少这一行，切换会话后计数器就会归零 ——
        哪怕 SSE 早就把正确的值推过来过（实测踩过这个坑）。
        """
        store = AlertStore()
        store.set_tokens({"total_tokens": 203841, "events": 40})
        snapshot = store.snapshot("watch.jsonl", "alerts.log")
        assert snapshot["tokens"]["total_tokens"] == 203841

    def test_normalize_alert_carries_token_fields(self) -> None:
        """归一化后的告警要带上 Token 相关字段。"""
        raw = {
            "alert_id": "x",
            "token_usage": {"total_tokens": 100},
            "token_saved": {"tokens": 900},
            "token_saved_text": "预估为您挽回 900 Token 的无效消耗",
        }
        normalized = normalize_alert(raw)
        assert normalized["token_usage"]["total_tokens"] == 100
        assert normalized["token_saved"]["tokens"] == 900
        assert "挽回" in normalized["token_saved_text"]

    def test_token_event_carries_dashboard_session_id(
        self, tmp_path: Any
    ) -> None:
        """多路模式下 Token 事件必须带**看板的**会话 ID。

        ⚠️ 这条是压测抓出来的真 bug：
        引擎写进账单行的 ``session_id`` 是 **Agent 名**（``codex``），
        跟看板的会话 ID（``stress-b``）是两套**不同的命名空间**。
        一旦被载荷里的旧值抢走，前端会判定「这条不属于当前会话」
        而直接丢弃 —— 结果就是**多路模式下 Token 计数器永远停在 0**。
        单路模式看不出来（没有「当前会话」的概念）。
        """
        from src.core.session_server import EventHub, Watcher

        hub = EventHub()
        queue = hub.subscribe()
        store = AlertStore()
        alert_log = tmp_path / "alerts.log"
        watcher = Watcher(
            str(tmp_path / "watch.jsonl"),
            str(alert_log),
            hub,
            store,
            session_id="stress-b",
        )
        payload = {
            # 引擎侧的会话名 —— 跟看板的 stress-b 不是一回事。
            "session_id": "codex",
            "input_tokens": 198462,
            "output_tokens": 5379,
            "total_tokens": 203841,
            "estimated": False,
        }
        alert_log.write_text(
            f"{TOKEN_MARKER} {json.dumps(payload)}\n", encoding="utf-8"
        )

        watcher._poll_once()

        events: List[Dict[str, Any]] = []
        while not queue.empty():
            events.append(queue.get_nowait())
        token_events = [
            item for item in events if item.get("type") == "tokens"
        ]
        assert token_events, "Token 账单行没有推成事件"
        assert token_events[0]["session_id"] == "stress-b"
        # 台账也要跟着更新（前端靠它刷新计数器）。
        assert store.tokens()["total_tokens"] == 203841


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
