"""Prompt 模板测试。

锁住从历史插件提取出来的几条核心约束，防止后续迭代把它们改丢。
"""

from __future__ import annotations

from src.llm.prompts import (
    EXPLAIN_OUTPUT_FIELDS,
    SENTINEL_EXPLAIN_TEMPLATE,
    build_explain_prompt,
)


def test_template_declares_fixed_json_contract():
    """输出字段固定，且不允许模型自由发挥。"""
    assert EXPLAIN_OUTPUT_FIELDS == ("title", "why", "touch", "outcome", "parts")
    assert "JSON 字段固定为：title, why, touch, outcome, parts" in (
        SENTINEL_EXPLAIN_TEMPLATE
    )


def test_template_forbids_re_judging_the_rules():
    """本地规则已判定的部分，模型不得重判、不得改写。"""
    assert "不要重判" in SENTINEL_EXPLAIN_TEMPLATE
    assert "不要改写" in SENTINEL_EXPLAIN_TEMPLATE


def test_template_keeps_evidence_priority():
    """证据优先级：Agent 自述 > 用户指令 > 命令实际行为。"""
    assert "证据优先级" in SENTINEL_EXPLAIN_TEMPLATE
    assert "Agent 的思考过程" in SENTINEL_EXPLAIN_TEMPLATE
    assert "用户最近的指令" in SENTINEL_EXPLAIN_TEMPLATE


def test_template_requires_gap_disclosure():
    """Agent 自述与实际行为不一致时，必须如实写出落差。"""
    assert "写出落差" in SENTINEL_EXPLAIN_TEMPLATE


def test_template_forbids_fabrication():
    """禁止编造：不确定时必须明说。"""
    assert "绝不允许编造" in SENTINEL_EXPLAIN_TEMPLATE
    assert "编造的理由比不写更危险" in SENTINEL_EXPLAIN_TEMPLATE


def test_template_excludes_death_penalty_judgement():
    """哨兵只观察告警，模板不得引导模型给出干预结论。"""
    assert "不要输出「建议终止」「建议放行」这类" in SENTINEL_EXPLAIN_TEMPLATE


def test_build_prompt_marks_rule_facts_as_settled():
    """规则层结论必须被标成「已判定，不要改写」。"""
    prompt = build_explain_prompt(
        tool_name="Bash",
        command="rm -rf /tmp/x",
        rule_facts={
            "verdict": "DANGEROUS",
            "rule_id": "dangerous_command",
            "level": "critical",
            "reason": "匹配到高危命令: rm -rf",
        },
    )

    assert "【哨兵规则层已判定（不要改写、不要重新评估）】" in prompt
    assert "判定=DANGEROUS" in prompt
    assert "命中规则=dangerous_command" in prompt


def test_build_prompt_forbids_invention_without_context():
    """没有会话上下文时，必须要求模型如实说明。"""
    prompt = build_explain_prompt(tool_name="Bash", context_available=False)

    assert "未读取到会话上下文" in prompt
    assert "仅翻译这一步本身" in prompt


def test_build_prompt_includes_thinking_and_user_instruction():
    prompt = build_explain_prompt(
        thinking="我要先删掉临时目录。",
        tool_name="Bash",
        command="rm -rf temp",
        user_instruction="把临时文件清理掉",
    )

    assert "【Agent 的思考过程】" in prompt
    assert "我要先删掉临时目录。" in prompt
    assert "【用户最近的指令】" in prompt
    assert "把临时文件清理掉" in prompt
