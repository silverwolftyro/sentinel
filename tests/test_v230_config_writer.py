"""配置文件原地写回测试（v2.3.0）。

核心红线：**绝不能整体重写配置文件** —— config/config.example.yaml 里
写满了中文注释，那是用户唯一的参考，用 yaml.safe_dump() 重写会把它们
全部抹掉。所以这里断言的每一处都是「只改值、其余原样」。

覆盖：标量 / 嵌套标量 / 块序列整块替换 / 序列项字段 / 找不到路径 /
空更新 / 重复路径 / 落盘与备份 / 文件不存在 / 无改动不写盘 / CRLF 保留 /
不支持的值类型。
"""

from __future__ import annotations

import os

import yaml

from src.config_loader import read_config_text, save_config, update_config_text

# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------
SAMPLE = """\
# 哨兵 Sentinel —— 主配置模板
# 这是**可入库的模板**
llm:
  # 提供方
  provider: "ollama"
  model: "qwen2.5:7b"   # 主力模型
  timeout: 20

rules:
  # 是否启用 L1 规则检测
  enabled: true
  # ---- 高危命令正则 ----
  dangerous_commands:
    - 'rm -rf'
    - 'sudo'
  # 置为 true 则用上面的列表**整体替换**内置表
  dangerous_commands_replace: false
  hot_reload:
    enabled: true
    interval: 1.0

alert:
  channels:
    # 远程叫醒服务
    - type: "webhook"
      enabled: false
      url: "http://old.example/hook"
      timeout: 10
"""


def _load(text):
    """把写回后的文本解析成 YAML —— 用来核对语义结果。"""
    return yaml.safe_load(text)


def _write(path, text):
    """按原文写文件（不做换行翻译），保证磁盘内容与传入文本一致。"""
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
# update_config_text
# ---------------------------------------------------------------------------
def test_scalar_keeps_comments():
    """改标量：新值生效，其它注释一字不动。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("llm.model", "deepseek-chat")]
    )
    assert applied == ["llm.model"]
    assert skipped == []
    assert 'model: "deepseek-chat"' in text
    # 注释必须原样保留
    assert "# 哨兵 Sentinel —— 主配置模板" in text
    assert "# 这是**可入库的模板**" in text
    assert "# 提供方" in text
    assert "# 主力模型" in text
    assert _load(text)["llm"]["model"] == "deepseek-chat"
    # 行尾注释仍在同一行
    assert 'model: "deepseek-chat"   # 主力模型' in text


def test_nested_scalar():
    """改三层嵌套的标量。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("rules.hot_reload.enabled", False)]
    )
    assert applied == ["rules.hot_reload.enabled"]
    assert skipped == []
    data = _load(text)
    assert data["rules"]["hot_reload"]["enabled"] is False
    assert data["rules"]["hot_reload"]["interval"] == 1.0
    assert "# ---- 高危命令正则 ----" in text
    assert "# 是否启用 L1 规则检测" in text


def test_block_sequence_replaced():
    """改块序列：整块替换，序列上方的注释保留。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("rules.dangerous_commands", ["rm -rf /", "curl | sh"])]
    )
    assert applied == ["rules.dangerous_commands"]
    assert skipped == []
    data = _load(text)
    assert data["rules"]["dangerous_commands"] == ["rm -rf /", "curl | sh"]
    # 旧序列被整块替换掉
    assert "'rm -rf'" not in text
    assert "'sudo'" not in text
    # 序列上方的注释保留
    assert "# ---- 高危命令正则 ----" in text
    assert "# 置为 true 则用上面的列表**整体替换**内置表" in text
    # 兄弟键没被动过
    assert data["rules"]["dangerous_commands_replace"] is False


def test_sequence_item_field():
    """改序列项里的字段：同一条目的其它字段保留。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("alert.channels[0].url", "http://new.example/hook")]
    )
    assert applied == ["alert.channels[0].url"]
    assert skipped == []
    data = _load(text)
    channel = data["alert"]["channels"][0]
    assert channel["url"] == "http://new.example/hook"
    # 同一条目的其它字段原样保留
    assert channel["type"] == "webhook"
    assert channel["enabled"] is False
    assert channel["timeout"] == 10
    assert "# 远程叫醒服务" in text


def test_sequence_item_inline_key():
    """改序列项内联映射的键（``- type: "webhook"``）。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("alert.channels[0].type", "dingtalk")]
    )
    assert applied == ["alert.channels[0].type"]
    assert skipped == []
    assert '- type: "dingtalk"' in text
    assert _load(text)["alert"]["channels"][0]["type"] == "dingtalk"


def test_missing_path_is_skipped():
    """找不到的路径：跳过，文本一字不改，也不新增键。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("llm.not_here", "x")]
    )
    assert applied == []
    assert skipped == ["llm.not_here"]
    assert text == SAMPLE
    assert "not_here" not in text


def test_empty_updates():
    """空更新：文本不变，两个列表都为空。"""
    text, applied, skipped = update_config_text(SAMPLE, [])
    assert text == SAMPLE
    assert applied == []
    assert skipped == []


def test_duplicate_path_uses_last_value():
    """同一路径出现多次：只应用最后一次。"""
    text, applied, skipped = update_config_text(
        SAMPLE,
        [("llm.model", "first"), ("llm.model", "second")],
    )
    assert applied == ["llm.model"]
    assert skipped == []
    assert _load(text)["llm"]["model"] == "second"
    assert "first" not in text


def test_unsupported_value_type_is_skipped():
    """列表里含 dict：跳过并记录，不抛异常，文本不变。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("rules.dangerous_commands", [{"a": 1}])]
    )
    assert applied == []
    assert skipped == ["rules.dangerous_commands"]
    assert text == SAMPLE


def test_mapping_value_is_skipped():
    """映射值：跳过并记录，不抛异常，文本不变。"""
    text, applied, skipped = update_config_text(
        SAMPLE, [("llm", {"model": "x"})]
    )
    assert applied == []
    assert skipped == ["llm"]
    assert text == SAMPLE


def test_one_failure_does_not_block_others():
    """单条失败不影响其它路径继续应用。"""
    text, applied, skipped = update_config_text(
        SAMPLE,
        [("llm.not_here", "x"), ("llm.model", "deepseek-chat")],
    )
    assert applied == ["llm.model"]
    assert skipped == ["llm.not_here"]
    assert _load(text)["llm"]["model"] == "deepseek-chat"


def test_real_example_config_is_untouched_by_missing_path():
    """真实模板里改一个深层键：其余内容原样保留。"""
    example = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "config.example.yaml",
    )
    if not os.path.exists(example):
        return
    original = read_config_text(example)
    assert original is not None
    text, applied, skipped = update_config_text(
        original, [("rules.hot_reload.enabled", False)]
    )
    assert applied == ["rules.hot_reload.enabled"]
    assert skipped == []
    assert _load(text)["rules"]["hot_reload"]["enabled"] is False
    # 模板里的中文注释一个都不能少
    for comment in (
        "# 哨兵 Sentinel —— 主配置模板",
        "# ---- 高危命令正则 ----",
        "# 置为 true 则用上面的列表**整体替换**内置表（默认增量追加）",
        "# ⚠️ 只热替换**规则引擎**（高危命令正则、红线类别、死循环阈值）。",
    ):
        assert comment in text
    assert len(text.splitlines()) == len(original.splitlines())


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------
def test_save_config_writes_and_backs_up(tmp_path):
    """落盘：新值生效、注释还在、``.bak`` 内容等于原文。"""
    path = tmp_path / "config.yaml"
    _write(path, SAMPLE)
    result = save_config(str(path), [("llm.model", "deepseek-chat")])
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["applied"] == ["llm.model"]
    assert result["skipped"] == []
    assert result["backup"] == str(path) + ".bak"

    content = path.read_text(encoding="utf-8")
    assert 'model: "deepseek-chat"' in content
    assert "# 提供方" in content
    assert _load(content)["llm"]["model"] == "deepseek-chat"

    backup = tmp_path / "config.yaml.bak"
    assert backup.exists()
    with open(str(backup), "r", encoding="utf-8", newline="") as handle:
        assert handle.read() == SAMPLE


def test_save_config_missing_file(tmp_path):
    """文件不存在：返回 ok=False 且带 error，不抛异常。"""
    missing = tmp_path / "nope" / "config.yaml"
    result = save_config(str(missing), [("llm.model", "x")])
    assert result["ok"] is False
    assert "error" in result
    assert "不存在" in result["error"]
    assert not missing.exists()


def test_save_config_no_change_does_not_write(tmp_path):
    """一个字都没改：不写文件、不产生备份、changed=False。"""
    path = tmp_path / "config.yaml"
    _write(path, SAMPLE)
    result = save_config(str(path), [("llm.not_here", "x")])
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["applied"] == []
    assert result["skipped"] == ["llm.not_here"]
    assert not (tmp_path / "config.yaml.bak").exists()
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_save_config_keeps_crlf(tmp_path):
    """写回不能改变原有换行风格。"""
    path = tmp_path / "config.yaml"
    path.write_bytes(SAMPLE.replace("\n", "\r\n").encode("utf-8"))
    result = save_config(str(path), [("llm.model", "deepseek-chat")])
    assert result["ok"] is True
    assert result["changed"] is True
    raw = path.read_bytes().decode("utf-8")
    assert "\r\n" in raw
    # 除行尾外没有裸的 \n
    assert raw.replace("\r\n", "").count("\n") == 0
    assert 'model: "deepseek-chat"' in raw


def test_save_config_readonly_is_not_fatal(tmp_path):
    """写回失败（只读）：返回 ok=False，不抛异常。"""
    path = tmp_path / "config.yaml"
    _write(path, SAMPLE)
    os.chmod(str(path), 0o444)
    try:
        result = save_config(str(path), [("llm.model", "deepseek-chat")])
    finally:
        os.chmod(str(path), 0o644)
    assert result["ok"] is False
    assert "error" in result
