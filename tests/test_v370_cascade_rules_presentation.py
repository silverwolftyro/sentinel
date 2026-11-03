"""v3.7.0「级联路由控制阀、规则管理页与极限摘要算法」测试。

这一版做了四件事，每一件都要被钉住
====================================

1. **L1 规则管理页** —— 规则清单能枚举、能单条关闭、能降级为「仅记录」，
   能整份导入 / 导出；
2. **级联路由控制阀** —— L1 / L2 / L3 各自独立开关，
   L2 关闭 + L3 开启时 L1 未命中的事件**直接桥接给云端 L3**，
   L1 命中后的拦截策略有四种取值；
3. **极限摘要算法** —— 100 档必须**真的合并**，
   连续 5 次读文件合成一行；
4. **布局清理** —— 主看板不再有档位滑条。

⚠️ 红线（R1-R9）与 v2.1.0 完全一致：
   本版只改「呈现 / 配置 / 交互」，
   **不改 L1/L2/L3 的判定规则、阈值与告警口径本身**。
   拦截策略属于用户显式要求的「动作层」调整，
   这里的测试把它与「判定层」分开钉住。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from src.config_loader import ensure_config_defaults
from src.core.engine import SentinelEngine
from src.llm.cascade import (
    CASCADE_STATUS_BRIDGED,
    CASCADE_STATUS_SINGLE,
    CascadeRouter,
)
from src.llm.factory import build_cascade_provider
from src.rules import registry

#: 仓库根目录（tests/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]

#: 内置高危命令条数（v3.6.0 起就是 18 条，一字未改）。
DANGEROUS_COUNT = 18

#: 内置红线类别数（root_delete / mass_delete / disk_wipe / remote_exec）。
CATASTROPHIC_COUNT = 4


def _read(*parts: str) -> str:
    """读仓库里的一个文本文件。"""
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")




# ---------------------------------------------------------------------------
# 一、规则清单模型
# ---------------------------------------------------------------------------
class TestRuleRegistry:
    """``src/rules/registry.py`` 的清单模型。"""

    def test_内置规则条数(self):
        """18 高危 + 4 红线 + 1 死循环 = 23 条。"""
        rules = registry.builtin_rules()
        assert len(rules) == DANGEROUS_COUNT + CATASTROPHIC_COUNT + 1
        assert len(registry.BUILTIN_DANGEROUS_RULES) == DANGEROUS_COUNT
        assert len(registry.BUILTIN_CATASTROPHIC_RULES) == CATASTROPHIC_COUNT
        assert len(registry.BUILTIN_LOOP_RULES) == 1

    def test_清单顺序是红线在前(self):
        """红线 → 高危 → 死循环 —— 红线永远排在最前面。"""
        kinds = [rule.kind for rule in registry.builtin_rules()]
        assert kinds[:CATASTROPHIC_COUNT] == ["catastrophic"] * CATASTROPHIC_COUNT
        assert kinds[CATASTROPHIC_COUNT] == "dangerous"
        assert kinds[-1] == "loop"

    def test_高危正则与旧版一字未改(self):
        """⚠️ 红线：**判定规则本身不能被这一版改动**。

        v3.6.1 的高危正则清单在 ``src/rules/basic.py`` 里，
        v3.7.0 的规则清单（``src/rules/registry.py``）必须与它**逐字相同** ——
        规则页上显示的「匹配内容」就是真正在跑的那一条。

        ⚠️ 这一条只能钉住「清单与匹配器不漂移」；
        「与 v3.6.1 的历史清单一致」由发布前的 git 比对保证
        （见 ``_管理文档/文档与报告/versions/`` 里的版本说明）。
        """
        from src.rules.basic import DEFAULT_DANGEROUS_COMMANDS

        old_patterns = list(DEFAULT_DANGEROUS_COMMANDS)
        assert old_patterns, "basic.py 里应当还有原始高危正则清单"
        new_patterns = [
            rule.pattern for rule in registry.BUILTIN_DANGEROUS_RULES
        ]
        assert new_patterns == old_patterns
        assert len(old_patterns) == DANGEROUS_COUNT

    def test_关键高危正则还在(self):
        """几条最要命的正则必须**逐字**还在清单里。"""
        patterns = [
            rule.pattern for rule in registry.BUILTIN_DANGEROUS_RULES
        ]
        assert r"\bsudo\b" in patterns
        assert r"\bmkfs(?:\.[a-z0-9]+)?\b" in patterns
        assert r"\bdd\b[^|;&\n]*\bof=/dev/" in patterns

    def test_关闭某条规则后不再生效(self):
        """单条 Toggle —— 关闭后这条规则不在清单的启用集合里。"""
        target = registry.BUILTIN_DANGEROUS_RULES[0].id
        selection = registry.RuleSelection.from_config(
            {"disabled_rules": [target]}
        )
        assert selection.is_enabled(target) is False
        assert set(selection.disabled) == {target}

    def test_仅记录降级(self):
        """降级为「仅记录」后动作变成 log，但规则**仍然启用**。"""
        target = registry.BUILTIN_DANGEROUS_RULES[0].id
        selection = registry.RuleSelection.from_config(
            {"log_only_rules": [target]}
        )
        assert selection.is_enabled(target) is True
        assert selection.is_log_only(target) is True
        assert selection.action_of(target) == registry.ACTION_LOG

    def test_默认动作是拦截(self):
        """没配置时动作就是「拦截」。"""
        selection = registry.RuleSelection.from_config({})
        assert selection.action_of(registry.BUILTIN_DANGEROUS_RULES[0].id) == (
            registry.ACTION_BLOCK
        )
        assert selection.l1_action == registry.L1_ACTION_RED_LINE_ONLY

    def test_非法拦截策略退回默认(self):
        """写错的策略名不能把引擎带歪 —— 退回「只拦红线级」。"""
        selection = registry.RuleSelection.from_config({"l1_action": "乱写"})
        assert selection.l1_action == registry.L1_ACTION_RED_LINE_ONLY

    def test_导出再导入不产生重复内置规则(self):
        """⚠️ 「导出 → 导入」是最容易出重复的路径。"""
        exported = registry.export_rules()
        text = json.dumps(exported, ensure_ascii=False)
        result = registry.import_rules(text)
        assert result["ok"] is True
        ids = [rule["id"] for rule in result["rules"]]
        assert len(ids) == len(set(ids))
        builtin_ids = {rule.id for rule in registry.builtin_rules()}
        assert builtin_ids.issubset(set(ids))

    def test_导入自定义规则只认高危(self):
        """自定义规则**只能**是高危命令 —— 红线与死循环是结构化判定。"""
        text = json.dumps(
            [
                {
                    "id": "custom.ok",
                    "name": "我的规则",
                    "pattern": "foo",
                    "level": "warn",
                    "kind": "dangerous",
                },
                {
                    "id": "custom.bad",
                    "name": "假红线",
                    "pattern": "bar",
                    "kind": "catastrophic",
                },
            ],
            ensure_ascii=False,
        )
        result = registry.import_rules(text)
        assert result["ok"] is True
        custom_ids = [rule["id"] for rule in result["custom"]]
        assert "custom.ok" in custom_ids
        assert "custom.bad" not in custom_ids

    def test_坏文本不抛异常(self):
        """坏 JSON / 坏 YAML 都只能被**拒绝**，不能把页面搞崩。"""
        result = registry.import_rules("这不是 JSON")
        assert result["ok"] is False
        assert result["errors"]

    def test_规则表包含全部三类(self):
        """规则表是红线类别的**唯一权威 UI** —— 三类都得列出来。"""
        kinds = {rule.kind for rule in registry.builtin_rules()}
        assert kinds == {"dangerous", "catastrophic", "loop"}

    def test_红线类别只有四个(self):
        """红线类别名是登记过的 —— 白名单里不能塞不存在的类别。"""
        from src.rules.basic import CATEGORY_LABELS

        assert set(CATEGORY_LABELS) == {
            "root_delete",
            "mass_delete",
            "disk_wipe",
            "remote_exec",
        }


# ---------------------------------------------------------------------------
# 二、级联控制阀与桥接
# ---------------------------------------------------------------------------
class _StubProvider:
    """假级联提供方 —— 只记录被问了什么。

    ⚠️ 级联路由调的是提供方的 ``chat(system, user)``，
    返回的原始文本由 ``parse_cascade_payload`` 解析成三值判定 ——
    所以这里必须返回**能被解析的 JSON**，而不是现成的结论对象。
    """

    def __init__(self, name: str, verdict: str = "ON_TRACK") -> None:
        self.name = name
        self.verdict = verdict
        self.calls: List[Dict[str, Any]] = []
        self.model = "stub-{}".format(name)

    def chat(self, system: str, user: str) -> str:
        """记录一次调用并返回固定的三值判定。"""
        self.calls.append({"system": system, "user": user})
        return json.dumps(
            {
                "verdict": self.verdict,
                "reason": "{} 单点结论".format(self.name),
            },
            ensure_ascii=False,
        )

    def close(self) -> None:
        """兼容接口 —— 什么都不用关。"""


class TestCascadeSwitch:
    """L1 / L2 / L3 独立开关与 L2→L3 桥接。"""

    def test_两个都关就没有级联(self):
        """L2、L3 全关 ⇒ 级联整体不存在。"""
        router = CascadeRouter(use_l2=False, use_l3=False)
        assert router.available is False

    def test_只留L3也可用(self):
        """L2 关、L3 开 ⇒ 级联可用（单盲）。"""
        router = CascadeRouter(
            l3=_StubProvider("l3"), use_l2=False, use_l3=True
        )
        assert router.available is True

    def test_只留L2也可用(self):
        """L3 关、L2 开 ⇒ 级联可用。"""
        router = CascadeRouter(
            l2=_StubProvider("l2"), use_l2=True, use_l3=False
        )
        assert router.available is True

    def test_没有模型就不算可用(self):
        """⚠️ 开关开着但**没配模型** ⇒ 这条路依然不工作。"""
        router = CascadeRouter(use_l2=False, use_l3=True)
        assert router.available is False

    def test_L3关掉时走单点(self):
        """L3 关闭 ⇒ 只剩 L2 一票（单点判定，不亮红灯）。"""
        router = CascadeRouter(
            l2=_StubProvider("l2", verdict="DRIFT"),
            use_l2=True,
            use_l3=False,
        )
        result = router.filter(summary="做了点事")
        assert result.status == CASCADE_STATUS_SINGLE
        assert result.red_light is False

    def test_L2关闭时L3直接复核(self):
        """⚠️ 核心用例：L2 关掉后，事件**跳过 L2**直接由 L3 复核。"""
        l3 = _StubProvider("l3", verdict="DRIFT")
        router = CascadeRouter(l3=l3, use_l2=False, use_l3=True)
        result = router.filter(summary="做了点事")
        assert l3.calls, "L3 必须被问到"
        assert result.status == CASCADE_STATUS_BRIDGED
        assert result.bridged is True

    def test_单盲结论永不亮红灯(self):
        """⚠️ 单盲只有一票 —— 不能凭一票就亮红灯。"""
        l3 = _StubProvider("l3", verdict="DRIFT")
        router = CascadeRouter(l3=l3, use_l2=False, use_l3=True)
        result = router.filter(summary="做了点事")
        assert result.red_light is False
        assert result.status == CASCADE_STATUS_BRIDGED

    def test_桥接开关需要前置条件(self):
        """``bridge_l1_to_l3`` 只有在 L2 关、L3 开时才成立。"""
        assert (
            CascadeRouter(
                l3=_StubProvider("l3"), use_l2=False, use_l3=True
            ).bridging
            is False
        )
        assert (
            CascadeRouter(
                l3=_StubProvider("l3"),
                use_l2=False,
                use_l3=True,
                bridge_l1_to_l3=True,
            ).bridging
            is True
        )
        assert (
            CascadeRouter(
                l3=_StubProvider("l3"),
                use_l2=True,
                use_l3=True,
                bridge_l1_to_l3=True,
            ).bridging
            is False
        )

    def test_统计里有四个新字段(self):
        """统计要能看出「哪一段被跳过了」。"""
        stats = CascadeRouter(use_l2=False, use_l3=True).stats()
        for key in (
            "use_l2",
            "use_l3",
            "bridge_l1_to_l3",
            "bridged",
        ):
            assert key in stats

    def test_单盲次数会被记账(self):
        """走过一次单盲 ⇒ 计数 +1（看板能看出「桥接了多少次」）。"""
        router = CascadeRouter(
            l3=_StubProvider("l3"), use_l2=False, use_l3=True
        )
        assert router.stats()["bridged"] == 0
        router.filter(summary="做了点事")
        assert router.stats()["bridged"] == 1

    def test_工厂在L2关闭时不装配L2(self):
        """⚠️ 「关掉 L2 省显存」必须是真的**不装**，不是装上再不用。"""
        config = {
            "llm": {
                "cascade": {
                    "enabled": True,
                    "l2": {"enabled": False},
                    "l3": {"enabled": True},
                }
            }
        }
        provider = build_cascade_provider(config)
        assert provider is not None
        assert provider.use_l2 is False
        assert provider.use_l3 is True

    def test_工厂在L2L3全关时返回None(self):
        """两个都关 ⇒ 根本没有级联提供方。"""
        config = {
            "llm": {
                "cascade": {
                    "enabled": True,
                    "l2": {"enabled": False},
                    "l3": {"enabled": False},
                }
            }
        }
        assert build_cascade_provider(config) is None

    def test_工厂忽略不成立的桥接(self):
        """L2 还开着时，桥接开关必须被**忽略**（不能假装成立）。"""
        config = {
            "llm": {
                "cascade": {
                    "enabled": True,
                    "l2": {"enabled": True},
                    "l3": {"enabled": True},
                    "bridge_l1_to_l3": True,
                }
            }
        }
        provider = build_cascade_provider(config)
        assert provider is not None
        assert provider.bridging is False


# ---------------------------------------------------------------------------
# 三、L1 命中后的拦截策略
# ---------------------------------------------------------------------------
class TestL1Action:
    """``SentinelEngine._l1_action`` / ``SentinelEngine._maybe_intervene``。"""

    def _engine(self, l1_action: str = registry.L1_ACTION_RED_LINE_ONLY) -> SentinelEngine:
        """造一个只带规则引擎的引擎。"""
        engine = SentinelEngine.__new__(SentinelEngine)
        engine.rule_engine = registry.RuleSelection.from_config(
            {"l1_action": l1_action}
        )
        # v3.7.0：规则引擎上挂的是 ``selection`` —— 这里补上。
        engine.rule_engine = _FakeRuleEngine(l1_action)
        engine.guardian = None
        engine._bridge_pending = {}
        return engine

    def test_策略取值有四种(self):
        """四种取值都在，且默认值是「只拦红线级」。"""
        assert set(registry.VALID_L1_ACTIONS) == {
            "red_line_only",
            "suspend",
            "alert",
            "escalate",
        }
        assert registry.L1_ACTION_RED_LINE_ONLY == "red_line_only"

    def test_默认只拦红线(self):
        """⚠️ 默认行为必须与 v3.6.0 **完全一致**。"""
        engine = self._engine()
        assert engine._l1_action() == registry.L1_ACTION_RED_LINE_ONLY

    def test_策略每次现读(self):
        """策略是**每次现读**的 —— 热重载后立即生效。"""
        engine = self._engine()
        assert engine._l1_action() == registry.L1_ACTION_RED_LINE_ONLY
        engine.rule_engine.selection = registry.RuleSelection.from_config(
            {"l1_action": "alert"}
        )
        assert engine._l1_action() == "alert"

    def test_只告警策略永不挂起(self):
        """「只告警不挂起」⇒ 守护者一次都不该被叫到。"""
        engine = self._engine("alert")
        engine._maybe_intervene(
            {
                "rule_id": "catastrophic_root_delete",
                "severity": "red_line",
                "rule_entry_id": "catastrophic.root_delete",
            }
        )
        assert engine.guardian is None

    def test_放行策略永不挂起(self):
        """「放行给 L2 二次判断」⇒ 本地**不**挂起。"""
        engine = self._engine("escalate")
        engine._maybe_intervene(
            {
                "rule_id": "catastrophic_root_delete",
                "severity": "red_line",
                "rule_entry_id": "catastrophic.root_delete",
            }
        )
        assert engine.guardian is None

    def test_仅记录永不挂起(self):
        """「仅记录」⇒ 命中仍报，但**不**挂起。"""
        engine = self._engine("suspend")
        engine.rule_engine.selection = registry.RuleSelection.from_config(
            {"l1_action": "suspend", "log_only_rules": ["dangerous.rm_rf"]}
        )
        engine._maybe_intervene(
            {
                "rule_id": "dangerous_command",
                "severity": "standard",
                "rule_entry_id": "dangerous.rm_rf",
                "log_only": True,
            }
        )
        assert engine.guardian is None


class _FakeRuleEngine:
    """假规则引擎 —— 只提供 v3.7.0 需要的那几个口。"""

    def __init__(self, l1_action: str) -> None:
        """按给定策略造一个选择。"""
        self.selection = registry.RuleSelection.from_config(
            {"l1_action": l1_action}
        )


# ---------------------------------------------------------------------------
# 四、配置补齐
# ---------------------------------------------------------------------------
class TestEnsureConfigDefaults:
    """``ensure_config_defaults`` —— 只补缺失，幂等。"""

    def test_补齐缺失键(self):
        """缺的键会被补上。"""
        text = "rules:\n  enabled: true\n"
        new_text, added, skipped = ensure_config_defaults(
            text, {"rules": {"l1_action": "red_line_only"}}
        )
        assert "l1_action" in new_text
        assert added
        assert not skipped

    def test_不覆盖已有值(self):
        """⚠️ 已有值**一个字都不能动**。"""
        text = "rules:\n  l1_action: alert\n"
        new_text, _added, _skipped = ensure_config_defaults(
            text, {"rules": {"l1_action": "red_line_only"}}
        )
        assert "alert" in new_text
        assert "red_line_only" not in new_text

    def test_幂等(self):
        """补过一次再补 ⇒ 没有任何新增。"""
        text = "rules:\n  enabled: true\n"
        defaults = {"rules": {"l1_action": "red_line_only"}}
        once, _a, _s = ensure_config_defaults(text, defaults)
        twice, added, _s = ensure_config_defaults(once, defaults)
        assert added == []
        assert once == twice

    def test_类型冲突跳过(self):
        """默认值是映射、现值是标量 ⇒ 跳过，不能把配置写坏。"""
        text = "rules: hello\n"
        new_text, _added, skipped = ensure_config_defaults(
            text, {"rules": {"l1_action": "red_line_only"}}
        )
        assert skipped
        assert yaml.safe_load(new_text)["rules"] == "hello"

    def test_补出来的配置能解析(self):
        """补完之后必须还是合法 YAML。"""
        text = "llm:\n  model: gpt\n"
        new_text, _added, _skipped = ensure_config_defaults(
            text,
            {
                "presentation": {
                    "log_compression": 40,
                    "audit_compression": 60,
                }
            },
        )
        parsed = yaml.safe_load(new_text)
        assert parsed["presentation"]["log_compression"] == 40

    def test_配置样例带齐新键(self):
        """``config/config.example.yaml`` 必须带齐 v3.7.0 的新键。"""
        parsed = yaml.safe_load(_read("config", "config.example.yaml"))
        assert "presentation" in parsed
        assert parsed["presentation"]["log_compression"] == 40
        assert parsed["presentation"]["audit_compression"] == 60
        assert parsed["rules"]["l1_action"] == "red_line_only"
        assert parsed["llm"]["cascade"]["l2"]["enabled"] is True
        assert parsed["llm"]["cascade"]["l3"]["enabled"] is True
        assert "bridge_l1_to_l3" in parsed["llm"]["cascade"]


# ---------------------------------------------------------------------------
# 五、HTTP 接口
# ---------------------------------------------------------------------------
class TestRulesApi:
    """``/api/rules`` 一族。"""











class TestPresentationApi:
    """``/api/presentation`` 与流水线开关。"""








# ---------------------------------------------------------------------------
# 六、前端契约
# ---------------------------------------------------------------------------
class TestFrontendContract:
    """看板、设置页与路由的前端契约。"""
















# ---------------------------------------------------------------------------
# 八、配置写回回归（v3.7.0 实机演示时发现）
# ---------------------------------------------------------------------------
#: PyYAML **默认输出**风格的配置 ——
#: 序列项与父键**同列**（``disabled_rules:`` 与 ``- xxx:`` 同缩进）。
#:
#: ⚠️ 这个风格曾把写回器坑惨了：老实现把「与父键同列的序列项」当成
#:    **父键的值块已经结束**，于是把已有列表当成「不存在」，
#:    **插入**了一份新列表、旧列表留在原地 ——
#:    同一个键出现两次 ⇒ ``ParserError: while parsing a block mapping``
#:    ⇒ 热重载降级、规则保存失效、档位回默认值。
PYYAML_STYLE = """\
# PyYAML 默认输出风格
llm:
  provider: "ollama"
rules:
  disabled_rules:
  - dangerous.sudo
  - dangerous.rm_rf
  log_only_rules:
  - dangerous.chmod_777
  hot_reload:
    enabled: true
alert:
  channels:
  - type: "webhook"
    enabled: false
    url: "http://old.example/hook"
"""


def test_缩进式序列被改后_yaml_仍可解析():
    """改「与父键同列」的序列：YAML 仍能解析，且不出现重复键。"""
    from src.config_loader import update_config_text

    text, applied, skipped = update_config_text(
        PYYAML_STYLE,
        [("rules.disabled_rules", ["dangerous.chmod_777"])],
    )
    assert applied == ["rules.disabled_rules"]
    assert skipped == []
    # ⚠️ 关键断言：写回后的文本**必须还能解析**。
    data = yaml.safe_load(text)
    assert data["rules"]["disabled_rules"] == ["dangerous.chmod_777"]
    # 同一个键不能出现两次 —— 出现两次就是「插入了一份新列表」。
    assert text.count("disabled_rules:") == 1
    # 兄弟键没被动过
    assert data["rules"]["log_only_rules"] == ["dangerous.chmod_777"]
    assert data["rules"]["hot_reload"]["enabled"] is True
    assert data["llm"]["provider"] == "ollama"


def test_缩进式序列项字段可改():
    """「与父键同列」的序列项里的字段也要能改。"""
    from src.config_loader import update_config_text

    text, applied, skipped = update_config_text(
        PYYAML_STYLE,
        [("alert.channels[0].url", "http://new.example/hook")],
    )
    assert applied == ["alert.channels[0].url"]
    assert skipped == []
    data = yaml.safe_load(text)
    channel = data["alert"]["channels"][0]
    assert channel["url"] == "http://new.example/hook"
    assert channel["type"] == "webhook"
    assert channel["enabled"] is False


def test_缩进式序列保持原有缩进风格():
    """写回时**保留原文件**的缩进风格，不无谓改动用户文件。"""
    from src.config_loader import update_config_text

    text, _, _ = update_config_text(
        PYYAML_STYLE,
        [("rules.disabled_rules", ["a", "b"])],
    )
    # 原文件就是「序列项与父键同列」⇒ 写回后也必须同列。
    assert '\n  - "a"\n' in text
    assert '\n    - "a"\n' not in text


def test_缩进式序列多次写回不累积重复键():
    """连着写回多次：键不会越攒越多。"""
    from src.config_loader import save_config

    path = ROOT / "logs" / "v370-config-writer-regression.yaml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PYYAML_STYLE, encoding="utf-8", newline="")
        for index in range(3):
            result = save_config(
                str(path),
                [("rules.disabled_rules", ["rule-{0}".format(index)])],
            )
            assert result["ok"] is True, result
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert data["rules"]["disabled_rules"] == [
                "rule-{0}".format(index)
            ]
            content = path.read_text(encoding="utf-8")
            assert content.count("disabled_rules:") == 1
    finally:
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# 九、红线基线未漂移
# ---------------------------------------------------------------------------
#: v3.6.1（``DEFAULT_DANGEROUS_COMMANDS``）的 18 条高危正则**原文**。
#:
#: ⚠️ 这一份是**基线快照**，不是「当前实现」的复制品 ——
#:    它的作用是：一旦有人动了某条正则（哪怕只是改一个 ``\s``），
#:    测试立刻失败，逼着改动者显式声明「我改了判定规则」。
#:    v3.7.0 只做「呈现 / 配置 / 交互」，
#:    **判定规则一字未动** —— 下面这条基线就是这句话的机器证明。
DANGEROUS_BASELINE_V361: List[str] = [
    r"\brm\s+(?:--recursive\b|--force\b|-\w*[rR]\w*)",
    r"\bsudo\b",
    r"\bchmod\s+(?:-[A-Za-z]+\s+)*777\b",
    r"\bgit\s+push\b[^|;&\n]*?(?:--force(?![-\w])|\s-f(?:orce)?(?![-\w]))",
    r"\brmdir\b[^|;&\n]*\s/s\b",
    r"\bdel\b\s+/[fsq]\b",
    r"\bRemove-Item\b[^|;&\n]*-Recurse",
    r"\bformat\b\s+[a-zA-Z]:",
    r"\bmkfs(?:\.[a-z0-9]+)?\b",
    r"\bdd\b[^|;&\n]*\bof=/dev/",
    r"\bInvoke-Expression\b|\biex\b",
    r"\bSet-ExecutionPolicy\b[^|;&\n]*\bUnrestricted\b",
    r"\bchown\b[^|;&\n]*\s-R\s+root\b",
    r"\btakeown\b",
    r"\bcurl\b[^|;&\n]*\|\s*(?:ba|z|k)?sh\b",
    r"\bwget\b[^|;&\n]*\|\s*(?:ba|z|k)?sh\b",
    r"\bhistory\s+-c\b",
    r"\bshutdown\b|\breboot\b|\bhalt\b",
]


def test_十八条高危正则逐字未动():
    """清单里的 18 条高危正则与 v3.6.1 基线**逐字一致**（含顺序）。"""
    actual = [entry.pattern for entry in registry.BUILTIN_DANGEROUS_RULES]
    assert len(actual) == DANGEROUS_COUNT
    assert actual == DANGEROUS_BASELINE_V361, (
        "高危正则被改动过 —— v3.7.0 只改呈现/配置/交互，判定规则不得变动。"
    )


def test_判定引擎仍在用同一批正则():
    """判定引擎实际编译的正则，也必须与基线一致（不只是清单显示一致）。"""
    from src.rules.basic import DEFAULT_DANGEROUS_COMMANDS

    assert list(DEFAULT_DANGEROUS_COMMANDS) == DANGEROUS_BASELINE_V361


def test_红线类别判定键未变():
    """红线的四个类别键与 v3.6.1 一致（``rule_id`` 契约不变）。"""
    from src.rules.basic import CATEGORY_LABELS

    assert set(CATEGORY_LABELS) == {
        "root_delete",
        "mass_delete",
        "disk_wipe",
        "remote_exec",
    }


# ---------------------------------------------------------------------------
# 十、版本
# ---------------------------------------------------------------------------
def test_版本号四处一致():
    """版本号在四处必须一致。

    ⚠️ **动态**断言 —— 写死版本号正是版本不同步的头号原因
    （v3.0.1 起 ``test_v300_release.py`` 就已经全面动态化）。
    """
    from src import __version__

    changelog = _read("CHANGELOG.md")
    assert __version__ in changelog
    assert "[v{}]".format(__version__) in changelog
