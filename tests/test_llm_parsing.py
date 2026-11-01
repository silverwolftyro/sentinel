"""L2 容错解析阶梯测试。

覆盖：正常 JSON、代码围栏、尾逗号、前后寒暄、嵌套括号、字符串里的花括号、
标注式文本兜底、完全无法识别。
"""

from __future__ import annotations

from src.llm.parsing import (
    extract_json_object,
    parse_explain_payload,
    parse_labelled_text,
    remove_trailing_commas,
    strip_noise,
)

VALID = (
    '{"title":"帮你补全缺失的模型文件","why":"你说要检查模型目录",'
    '"touch":"D:\\\\models 目录（只读）","outcome":"补齐后可以直接跑图",'
    '"parts":[]}'
)


def test_parse_plain_json():
    result = parse_explain_payload(VALID)

    assert result.ok
    assert result.mode == "json"
    assert result.get("title") == "帮你补全缺失的模型文件"
    assert result.get("outcome") == "补齐后可以直接跑图"


def test_parse_json_inside_code_fence():
    raw = "```json\n" + VALID + "\n```"

    result = parse_explain_payload(raw)

    assert result.ok
    assert result.get("title") == "帮你补全缺失的模型文件"


def test_parse_json_with_chitchat_around():
    raw = "好的，我来帮你分析：\n" + VALID + "\n希望这个解释有帮助！"

    result = parse_explain_payload(raw)

    assert result.ok
    assert result.get("title") == "帮你补全缺失的模型文件"


def test_parse_json_with_trailing_comma():
    raw = '{"title":"测试","why":"原因","touch":"影响","outcome":"后果",}'

    result = parse_explain_payload(raw)

    assert result.ok
    assert result.get("title") == "测试"


def test_extract_json_object_handles_braces_inside_strings():
    text = '{"title":"有个 { 花括号 } 在里面","why":"x"}'

    extracted = extract_json_object(text)

    assert extracted is not None
    assert extracted.startswith('{"title"')
    assert extracted.endswith("}")


def test_extract_json_object_handles_nested_objects():
    text = '{"parts":[{"label":"第 1 步","text":"做点事"}],"title":"标题"}'

    extracted = extract_json_object(text)

    assert extracted is not None
    assert '"parts"' in extracted


def test_parse_labelled_text_fallback():
    """JSON 崩了但模型退化成标注式文本时，仍要能捞出来。"""
    raw = "title: 帮你清理临时文件\nwhy: 你说要把缓存清掉\n"

    result = parse_explain_payload(raw)

    assert result.ok
    assert result.mode == "labelled"
    assert result.get("title") == "帮你清理临时文件"
    assert result.get("why") == "你说要把缓存清掉"


def test_parse_labelled_text_handles_markdown_and_quotes():
    raw = '- **title**: "带强调和引号的标题"\n'

    data = parse_labelled_text(raw)

    assert data["title"] == "带强调和引号的标题"


def test_parse_fails_gracefully_on_garbage():
    result = parse_explain_payload("模型今天不想说话。")

    assert result.ok is False
    assert result.mode == "failed"
    assert result.reason
    assert result.raw == "模型今天不想说话。"


def test_parse_fails_on_empty_output():
    result = parse_explain_payload("")

    assert result.ok is False
    assert result.reason == "模型输出为空"


def test_strip_noise_removes_bom_and_fence():
    assert strip_noise("\ufeff  ```json\n{}\n```  ") == "{}"


def test_remove_trailing_commas():
    assert remove_trailing_commas('{"a":1,}') == '{"a":1}'
    assert remove_trailing_commas('{"a":[1,2,],}') == '{"a":[1,2]}'
