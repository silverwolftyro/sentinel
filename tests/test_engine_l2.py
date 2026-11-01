"""L2 集成测试 —— SentinelEngine 与本地模型层的串联。

覆盖：解释入告警、模型异常不影响主流程、无模型客户端时的行为、
以及「Agent 自述」作为证据的传递。
"""

from __future__ import annotations

import json

from src.adapters.mcode import McodeAdapter
from src.core.engine import SentinelEngine
from src.rules.basic import RuleEngine


class Explainer:
    """假的 L2 客户端。"""

    def __init__(self, text="帮你把临时目录删掉", degraded=False, raises=False):
        self.text = text
        self.degraded = degraded
        self.raises = raises
        self.payloads = []

    def explain(self, payload):
        self.payloads.append(payload)
        if self.raises:
            raise RuntimeError("模型炸了")
        return type(
            "Explanation",
            (),
            {
                "text": self.text,
                "degraded": self.degraded,
                "reason": "降级原因" if self.degraded else "",
            },
        )()


LOG_LINES = [
    {"type": "thinking", "thinking": "用户让我清理临时构建目录。"},
    {
        "type": "tool_call",
        "tool_call": {"name": "Bash", "arguments": {"command": "rm -rf /tmp/build"}},
    },
]


def _write_jsonl(tmp_path, lines):
    path = tmp_path / "stream.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _engine(tmp_path, llm_client):
    return SentinelEngine(
        McodeAdapter(),
        RuleEngine(config={}),
        alert_log_path=str(tmp_path / "sentinel_alert.log"),
        console=False,
        llm_client=llm_client,
    )


def test_alert_carries_human_readable_explanation(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)
    explainer = Explainer(text="帮你把临时构建目录删掉")

    engine = _engine(tmp_path, explainer)
    try:
        summary = engine.run(path)
    finally:
        engine.close()

    assert len(summary["alerts"]) == 1
    alert = summary["alerts"][0]
    assert alert["explanation"] == "帮你把临时构建目录删掉"
    assert alert["explanation_degraded"] is False


def test_explanation_is_written_to_alert_log(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)
    explainer = Explainer(text="帮你把临时构建目录删掉")

    engine = _engine(tmp_path, explainer)
    try:
        engine.run(path)
    finally:
        engine.close()

    content = (tmp_path / "sentinel_alert.log").read_text(encoding="utf-8")
    assert "人话解释" in content
    assert "帮你把临时构建目录删掉" in content


def test_agent_thinking_is_forwarded_as_evidence(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)
    explainer = Explainer()

    engine = _engine(tmp_path, explainer)
    try:
        engine.run(path)
    finally:
        engine.close()

    assert explainer.payloads
    assert explainer.payloads[0]["thinking"] == "用户让我清理临时构建目录。"
    assert explainer.payloads[0]["verdict"]["verdict"] == "DANGEROUS"


def test_llm_failure_does_not_crash_the_engine(tmp_path):
    """L2 抛异常时，哨兵必须继续跑完并照常告警。"""
    path = _write_jsonl(tmp_path, LOG_LINES)
    explainer = Explainer(raises=True)

    engine = _engine(tmp_path, explainer)
    try:
        summary = engine.run(path)
    finally:
        engine.close()

    assert len(summary["alerts"]) == 1
    alert = summary["alerts"][0]
    assert alert["explanation_degraded"] is True
    assert "RuntimeError" in alert["explanation_reason"]


def test_engine_without_llm_client_still_alerts(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)

    engine = _engine(tmp_path, None)
    try:
        summary = engine.run(path)
    finally:
        engine.close()

    alert = summary["alerts"][0]
    assert alert["explanation"] == ""
    assert alert["explanation_degraded"] is False
    # 规则层原始判定依然完整
    assert "匹配到高危命令" in alert["reason"]


def test_degraded_explanation_still_reaches_the_log(tmp_path):
    path = _write_jsonl(tmp_path, LOG_LINES)
    explainer = Explainer(text="规则判定：匹配到高危命令", degraded=True)

    engine = _engine(tmp_path, explainer)
    try:
        engine.run(path)
    finally:
        engine.close()

    content = (tmp_path / "sentinel_alert.log").read_text(encoding="utf-8")
    assert "规则判定" in content
