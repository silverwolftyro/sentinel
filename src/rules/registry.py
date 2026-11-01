"""L1 规则清单（v3.7.0）。

v3.6.x 的 L1 规则是**写死**的：内置高危命令正则是一个匿名元组
（``src/rules/basic.py`` 的 ``DEFAULT_DANGEROUS_COMMANDS``），
红线类别是四个常量，用户能做的只有「整表替换 / 追加正则」。

这一版把它变成**可枚举、可开关、可降级**的清单：

- **可枚举** —— 每条规则有稳定的 ``id``、中文名、匹配内容、风险等级，
  设置页能列成表格；
- **可开关** —— 用户可以单独关掉不常用的一条；
- **可降级** —— 「拦截」可以降成「仅记录」（照样告警，但不挂起）；
- **可导入导出** —— JSON / YAML 文本互通。

三种规则的差异
================

``dangerous``
    高危命令正则。命中即 ``DANGEROUS / standard``（告警即可）。
``catastrophic``
    红线级类别。命中即 ``DANGEROUS / red_line`` —— **红线是守护者
    挂起的唯一触发源**。
``loop``
    死循环检测。跨事件累积的状态机，命中即 ``LOOP / standard``。

⚠️ **本模块不改变任何判定逻辑** ——
只负责「有哪些规则」「哪条开着」「命中后按什么动作处理」。
真正的判定仍在 :mod:`src.rules.basic`。

配置形态
========

新增的三段都刻意用**列表**存（不是映射），
因为配置写回器（:mod:`src.config_loader`）只支持标量与标量列表 ——
用映射就写不回去了。

.. code-block:: yaml

    rules:
      l1_action: "red_line_only"   # L1 命中后的拦截策略
      disabled_rules: []           # 被关闭的规则 id
      log_only_rules: []           # 被降级为「仅记录」的规则 id
      custom_rules: []             # 自定义规则；每项是一段 JSON 文本

``custom_rules`` 的每一项都是**一段 JSON 文本**（而不是 YAML 映射），
这样才能被写回器原样落盘；读取时两种形态都接受。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

#: 本模块的日志器（库代码一律用 logging，不直接 print）。
logger = logging.getLogger(__name__)

#: 规则动作 —— 命中后「拦截」（可挂起）。
ACTION_BLOCK = "block"
#: 规则动作 —— 命中后「仅记录」（告警，但不挂起）。
ACTION_LOG = "log"

#: 风险等级 —— 最高。
LEVEL_CRITICAL = "critical"
#: 风险等级 —— 中。
LEVEL_WARN = "warn"
#: 风险等级 —— 低。
LEVEL_INFO = "info"

#: 规则分类 —— 高危命令。
KIND_DANGEROUS = "dangerous"
#: 规则分类 —— 红线级。
KIND_CATASTROPHIC = "catastrophic"
#: 规则分类 —— 死循环。
KIND_LOOP = "loop"

#: 合法动作集合。
VALID_ACTIONS: Tuple[str, ...] = (ACTION_BLOCK, ACTION_LOG)

#: 合法等级集合。
VALID_LEVELS: Tuple[str, ...] = (LEVEL_CRITICAL, LEVEL_WARN, LEVEL_INFO)

#: 合法规则分类集合。
VALID_KINDS: Tuple[str, ...] = (KIND_DANGEROUS, KIND_CATASTROPHIC, KIND_LOOP)

# ---------------------------------------------------------------------------
# L1 命中后的拦截策略（v3.7.0）
# ---------------------------------------------------------------------------

#: 只有红线才挂起 —— **默认值**，与 v3.6.x 的现状**完全等价**。
L1_ACTION_RED_LINE_ONLY = "red_line_only"
#: L1 判为「危险」就挂起（不再只认红线）。
L1_ACTION_SUSPEND = "suspend"
#: 只告警，**绝不挂起**。
L1_ACTION_ALERT = "alert"
#: 先不挂起，交给 L2 二次判断（级联双盲命中后才挂起）。
L1_ACTION_ESCALATE = "escalate"

#: 合法拦截策略集合。
VALID_L1_ACTIONS: Tuple[str, ...] = (
    L1_ACTION_RED_LINE_ONLY,
    L1_ACTION_SUSPEND,
    L1_ACTION_ALERT,
    L1_ACTION_ESCALATE,
)

#: 各拦截策略的中文说明（设置页与文档共用一份，避免两处说法打架）。
L1_ACTION_LABELS: Dict[str, str] = {
    L1_ACTION_RED_LINE_ONLY: "仅红线挂起（默认，等同现状）",
    L1_ACTION_SUSPEND: "命中即挂起",
    L1_ACTION_ALERT: "只告警不挂起",
    L1_ACTION_ESCALATE: "放行给 L2 二次判断",
}


@dataclass(frozen=True)
class RuleEntry:
    """一条 L1 规则的清单条目。

    :param id:       稳定标识（导入导出、开关、动作都用它）。
    :param name:     中文名（界面显示用）。
    :param pattern:  匹配内容（正则原文，或该类别的判定说明）。
    :param level:    风险等级 —— ``critical`` / ``warn`` / ``info``。
    :param kind:     规则分类 —— ``dangerous`` / ``catastrophic`` / ``loop``。
    :param severity: 命中后的严重度 —— ``red_line`` 或 ``standard``。
    :param source:   来源 —— ``builtin`` 或 ``custom``。
    """

    id: str
    name: str
    pattern: str
    level: str
    kind: str
    severity: str
    source: str = "builtin"

    def as_dict(self, *, enabled: bool = True, action: str = ACTION_BLOCK) -> Dict[str, Any]:
        """转成可 JSON 序列化的字典。

        :param enabled: 是否启用。
        :param action:  命中动作（``block`` / ``log``）。
        :return: 字典。
        """
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "level": self.level,
            "kind": self.kind,
            "severity": self.severity,
            "source": self.source,
            "enabled": bool(enabled),
            "action": action,
        }


# ---------------------------------------------------------------------------
# 内置清单
# ---------------------------------------------------------------------------

#: 内置高危命令清单（顺序与判定优先级一致）。
#:
#: ⚠️ 正则**一字未改** —— 全部来自 v3.6.0 的
#: ``DEFAULT_DANGEROUS_COMMANDS``，只补了名字与稳定 id。
BUILTIN_DANGEROUS_RULES: Tuple[RuleEntry, ...] = (
    RuleEntry(
        "dangerous.rm_rf",
        "递归强删（rm -rf / --recursive / --force）",
        r"\brm\s+(?:--recursive\b|--force\b|-\w*[rR]\w*)",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.sudo",
        "提权执行（sudo）",
        r"\bsudo\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.chmod_777",
        "全员可写（chmod 777）",
        r"\bchmod\s+(?:-[A-Za-z]+\s+)*777\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.git_push_force",
        "强推覆盖远端（git push -f / --force）",
        r"\bgit\s+push\b[^|;&\n]*?(?:--force(?![-\w])|\s-f(?:orce)?(?![-\w]))",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.rmdir_s",
        "整目录删除（rmdir /s）",
        r"\brmdir\b[^|;&\n]*\s/s\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.del_fsq",
        "强制删除（del /f /s /q）",
        r"\bdel\b\s+/[fsq]\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.remove_item_recurse",
        "递归删除（Remove-Item -Recurse）",
        r"\bRemove-Item\b[^|;&\n]*-Recurse",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.format_drive",
        "格式化磁盘（format X:）",
        r"\bformat\b\s+[a-zA-Z]:",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.mkfs",
        "重建文件系统（mkfs）",
        r"\bmkfs(?:\.[a-z0-9]+)?\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.dd_to_device",
        "覆写块设备（dd of=/dev/）",
        r"\bdd\b[^|;&\n]*\bof=/dev/",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.invoke_expression",
        "动态执行（Invoke-Expression / iex）",
        r"\bInvoke-Expression\b|\biex\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.set_execution_policy",
        "放开执行策略（Set-ExecutionPolicy Unrestricted）",
        r"\bSet-ExecutionPolicy\b[^|;&\n]*\bUnrestricted\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.chown_root",
        "递归改属主为 root（chown -R root）",
        r"\bchown\b[^|;&\n]*\s-R\s+root\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.takeown",
        "夺取文件属主（takeown）",
        r"\btakeown\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.curl_pipe_sh",
        "下载即执行（curl … | sh）",
        r"\bcurl\b[^|;&\n]*\|\s*(?:ba|z|k)?sh\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.wget_pipe_sh",
        "下载即执行（wget … | sh）",
        r"\bwget\b[^|;&\n]*\|\s*(?:ba|z|k)?sh\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.history_clear",
        "清历史痕迹（history -c）",
        r"\bhistory\s+-c\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
    RuleEntry(
        "dangerous.shutdown",
        "关机重启（shutdown / reboot / halt）",
        r"\bshutdown\b|\breboot\b|\bhalt\b",
        LEVEL_CRITICAL, KIND_DANGEROUS, "standard",
    ),
)

#: 内置红线类别清单。
#:
#: ⚠️ 只有这**四条**是真实实现的（``src/rules/basic.py`` 的
#: ``CATEGORY_LABELS``）—— v3.6.x 的设置页里另外四个勾选框
#: （提权 / 网络变更 / 清历史 / 读凭据）从来**没有**对应判定逻辑，
#: 勾上也不生效。v3.7.0 起设置页只列真实存在的规则。
BUILTIN_CATASTROPHIC_RULES: Tuple[RuleEntry, ...] = (
    RuleEntry(
        "catastrophic.root_delete",
        "递归删除根目录 / 家目录 / 整盘",
        "根路径目标（/ ~/ C:\\ 等）上的递归删除",
        LEVEL_CRITICAL, KIND_CATASTROPHIC, "red_line",
    ),
    RuleEntry(
        "catastrophic.mass_delete",
        "批量删除大量文件",
        "通配符删除 / find -delete / git clean -fdx",
        LEVEL_CRITICAL, KIND_CATASTROPHIC, "red_line",
    ),
    RuleEntry(
        "catastrophic.disk_wipe",
        "整盘抹除或覆写块设备",
        "format / mkfs / dd of=/dev/ / shred /dev/",
        LEVEL_CRITICAL, KIND_CATASTROPHIC, "red_line",
    ),
    RuleEntry(
        "catastrophic.remote_exec",
        "下载远程内容直接执行",
        "curl … | sh / curl … | python",
        LEVEL_CRITICAL, KIND_CATASTROPHIC, "red_line",
    ),
)

#: 内置死循环规则清单。
BUILTIN_LOOP_RULES: Tuple[RuleEntry, ...] = (
    RuleEntry(
        "loop.repeat_read",
        "重复读取同一文件（疑似死循环）",
        "连续读取同一文件达到阈值即判定",
        LEVEL_WARN, KIND_LOOP, "standard",
    ),
)


def builtin_rules() -> Tuple[RuleEntry, ...]:
    """全部内置规则。

    :return: 规则条目元组（顺序：红线 → 高危 → 死循环）。
    """
    return (
        BUILTIN_CATASTROPHIC_RULES
        + BUILTIN_DANGEROUS_RULES
        + BUILTIN_LOOP_RULES
    )


# ---------------------------------------------------------------------------
# 用户选择（开关 + 降级 + 自定义规则）
# ---------------------------------------------------------------------------


class RuleSelection:
    """用户在配置里对规则做出的选择。

    配置形态（``rules`` 节）::

        rules:
          l1_action: "red_line_only"
          disabled_rules: ["dangerous.sudo"]
          log_only_rules: ["catastrophic.root_delete"]
          custom_rules:
            - '{"id": "custom.my_rule", "name": "我的规则",
               "pattern": "\\\\bfoo\\\\b", "level": "warn"}'

    :param disabled: 被关闭的规则 id 集合。
    :param log_only: 被降级为「仅记录」的规则 id 集合。
    :param custom: 用户自定义规则条目。
    :param l1_action: L1 命中后的拦截策略。
    """

    def __init__(
        self,
        *,
        disabled: Optional[Iterable[str]] = None,
        log_only: Optional[Iterable[str]] = None,
        custom: Optional[Iterable[RuleEntry]] = None,
        l1_action: str = L1_ACTION_RED_LINE_ONLY,
    ) -> None:
        self.disabled = frozenset(
            str(item).strip() for item in (disabled or []) if str(item).strip()
        )
        self.log_only = frozenset(
            str(item).strip() for item in (log_only or []) if str(item).strip()
        )
        self.custom: Tuple[RuleEntry, ...] = tuple(custom or ())
        action = str(l1_action or L1_ACTION_RED_LINE_ONLY).strip().lower()
        self.l1_action = (
            action if action in VALID_L1_ACTIONS else L1_ACTION_RED_LINE_ONLY
        )

    @classmethod
    def from_config(cls, rules_config: Optional[Mapping[str, Any]]) -> "RuleSelection":
        """从配置字典构造。

        :param rules_config: ``rules`` 节。
        :return: :class:`RuleSelection`。
        """
        data = rules_config if isinstance(rules_config, Mapping) else {}
        disabled = _string_list(data.get("disabled_rules"))
        log_only = _string_list(data.get("log_only_rules"))
        custom, _errors = parse_custom_rules(data.get("custom_rules"))
        return cls(
            disabled=disabled,
            log_only=log_only,
            custom=custom,
            l1_action=str(data.get("l1_action") or L1_ACTION_RED_LINE_ONLY),
        )

    def is_enabled(self, rule_id: str) -> bool:
        """某条规则是否启用。

        :param rule_id: 规则 id。
        :return: 启用返回 ``True``。
        """
        return str(rule_id) not in self.disabled

    def action_of(self, rule_id: str) -> str:
        """某条规则命中后的动作。

        :param rule_id: 规则 id。
        :return: ``block``（默认）或 ``log``。
        """
        if str(rule_id) in self.log_only:
            return ACTION_LOG
        return ACTION_BLOCK

    def is_log_only(self, rule_id: str) -> bool:
        """某条规则是否被降级成「仅记录」。

        :param rule_id: 规则 id。
        :return: 是「仅记录」返回 ``True``。
        """
        return self.action_of(rule_id) == ACTION_LOG

    def entries(self) -> Tuple[RuleEntry, ...]:
        """全部规则条目（内置 + 自定义）。

        :return: 规则条目元组。
        """
        return builtin_rules() + self.custom

    def by_id(self, rule_id: str) -> Optional[RuleEntry]:
        """按 id 找规则。

        :param rule_id: 规则 id。
        :return: 规则条目；找不到返回 ``None``。
        """
        for entry in self.entries():
            if entry.id == rule_id:
                return entry
        return None

    def disabled_categories(self) -> frozenset:
        """被关闭的红线类别（不含 ``catastrophic.`` 前缀）。

        :return: 类别名集合。
        """
        names = set()
        for rule_id in self.disabled:
            if rule_id.startswith("catastrophic."):
                names.add(rule_id.split(".", 1)[1])
        return frozenset(names)


# ---------------------------------------------------------------------------
# 导入 / 导出
# ---------------------------------------------------------------------------


def export_rules(selection: Optional[RuleSelection] = None) -> List[Dict[str, Any]]:
    """导出当前全部规则（含开关与动作）。

    :param selection: 用户选择；为 ``None`` 时用默认（全开、全拦截）。
    :return: 可 JSON 序列化的规则字典列表。
    """
    sel = selection or RuleSelection()
    return [
        entry.as_dict(
            enabled=sel.is_enabled(entry.id),
            action=sel.action_of(entry.id),
        )
        for entry in sel.entries()
    ]


def parse_custom_rules(raw: Any) -> Tuple[Tuple[RuleEntry, ...], List[str]]:
    """解析用户自定义规则。

    每一项可以是**映射**（手写 YAML 的常见写法）或**一段 JSON 文本**
    （配置写回器能原样落盘的那种形态）—— 两种都接受。

    ⚠️ 自定义规则**只能是高危命令**（``dangerous``）——
    红线与死循环的判定是结构化的（路径形状、跨事件状态机），
    一条正则表达不出来，所以这里**明确拒绝**而不是收下不生效。

    :param raw: 列表（每项是字典或 JSON 文本）。
    :return: ``(条目元组, 错误信息列表)``；非法项被跳过并记下原因。
    """
    entries: List[RuleEntry] = []
    errors: List[str] = []
    if not isinstance(raw, (list, tuple)):
        return (), errors
    for index, item in enumerate(raw):
        row = _as_mapping(item)
        if row is None:
            errors.append("第 {} 条不是合法的规则对象，已跳过。".format(index + 1))
            continue
        rule_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        pattern = str(row.get("pattern") or "").strip()
        if not rule_id:
            errors.append("第 {} 条缺少 id，已跳过。".format(index + 1))
            continue
        if not name:
            name = rule_id
        if not pattern:
            errors.append("规则 {} 缺少匹配内容，已跳过。".format(rule_id))
            continue
        kind = str(row.get("kind") or KIND_DANGEROUS).strip().lower()
        if kind != KIND_DANGEROUS:
            errors.append(
                "规则 {} 的类型「{}」不受支持（自定义规则只能是高危命令），"
                "已跳过。".format(rule_id, kind)
            )
            continue
        level = str(row.get("level") or LEVEL_WARN).strip().lower()
        if level not in VALID_LEVELS:
            level = LEVEL_WARN
        entries.append(
            RuleEntry(
                id=rule_id,
                name=name,
                pattern=pattern,
                level=level,
                kind=KIND_DANGEROUS,
                severity="standard",
                source="custom",
            )
        )
    return tuple(entries), errors


def dump_custom_rules(entries: Sequence[RuleEntry]) -> List[str]:
    """把自定义规则转成**可写回**的形态（每项一段 JSON 文本）。

    配置写回器只支持标量与标量列表 —— 映射写不回去，
    所以落盘时每条规则压成一段 JSON 文本。

    :param entries: 自定义规则条目。
    :return: JSON 文本列表。
    """
    return [
        json.dumps(
            {
                "id": entry.id,
                "name": entry.name,
                "pattern": entry.pattern,
                "level": entry.level,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for entry in entries
    ]


def import_rules(text: str) -> Dict[str, Any]:
    """从 JSON / YAML 文本导入规则。

    支持两种载荷：

    - **整表**（导出格式）—— ``[{"id": …, "enabled": …, "action": …}]``
      或 ``{"rules": [...]}``；
    - **增量**（只要规则本体）—— 同样接受 ``{"rules": [...]}``。

    :param text: 文本内容。
    :return: 结果字典，含 ``ok`` / ``rules`` / ``disabled`` /
        ``log_only`` / ``custom`` / ``errors``。
    """
    result: Dict[str, Any] = {
        "ok": False,
        "rules": [],
        "disabled": [],
        "log_only": [],
        "custom": [],
        "errors": [],
    }
    raw_text = (text or "").strip()
    if not raw_text:
        result["errors"].append("内容为空。")
        return result

    data = _parse_rule_text(raw_text)
    if data is None:
        result["errors"].append("既不是合法 JSON，也不是合法 YAML。")
        return result
    if isinstance(data, Mapping):
        rows = data.get("rules")
        if rows is None:
            rows = data.get("items")
        if rows is None:
            result["errors"].append("找不到 rules 列表。")
            return result
    elif isinstance(data, (list, tuple)):
        rows = list(data)
    else:
        result["errors"].append("规则内容必须是列表或带 rules 的对象。")
        return result
    if not isinstance(rows, (list, tuple)):
        result["errors"].append("rules 必须是列表。")
        return result

    disabled: List[str] = []
    log_only: List[str] = []
    custom_raw: List[Any] = []
    errors: List[str] = []
    builtin_ids = {entry.id for entry in builtin_rules()}
    for index, row in enumerate(rows):
        item = _as_mapping(row)
        if item is None:
            errors.append("第 {} 条不是字典，已跳过。".format(index + 1))
            continue
        rule_id = str(item.get("id") or "").strip()
        if not rule_id:
            errors.append("第 {} 条缺少 id，已跳过。".format(index + 1))
            continue
        enabled = item.get("enabled")
        if enabled is not None and not bool(enabled):
            disabled.append(rule_id)
        action = str(item.get("action") or "").strip().lower()
        if action == ACTION_LOG:
            log_only.append(rule_id)
        elif action and action != ACTION_BLOCK:
            errors.append(
                "规则 {} 的动作「{}」不合法，已忽略。".format(rule_id, action)
            )
        # 内置规则的开关状态生效，但**不会**被收成自定义规则 ——
        # 否则「导出再导入」会把 23 条内置规则复制成 23 条自定义规则。
        if rule_id not in builtin_ids and (
            "pattern" in item or "name" in item
        ):
            custom_raw.append(dict(item))

    custom, custom_errors = parse_custom_rules(custom_raw)
    errors.extend(custom_errors)
    selection = RuleSelection(
        disabled=disabled, log_only=log_only, custom=custom
    )
    result.update(
        {
            "ok": True,
            "rules": export_rules(selection),
            "disabled": disabled,
            "log_only": log_only,
            "custom": [entry.as_dict() for entry in custom],
            "errors": errors,
        }
    )
    return result


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _string_list(raw: Any) -> List[str]:
    """把配置里的列表读成字符串列表。

    :param raw: 原始值。
    :return: 去空白、去空串后的字符串列表。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _as_mapping(item: Any) -> Optional[Dict[str, Any]]:
    """把一项归一成字典（映射或 JSON 文本都认）。

    :param item: 原始项。
    :return: 字典；无法解析返回 ``None``。
    """
    if isinstance(item, Mapping):
        return dict(item)
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if isinstance(data, Mapping):
            return dict(data)
    return None


def _parse_yaml_text(text: str) -> Any:
    """把文本解析成 YAML 数据。

    :param text: 文本内容。
    :return: 解析结果；PyYAML 不可用或文本不合法时返回 ``None``。
    """
    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML 不可用，只能按 JSON 解析规则文本。")
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _parse_rule_text(text: str) -> Any:
    """把文本解析成 JSON 或 YAML 数据。

    ⚠️ JSON 不成立时**换 YAML 再试一次** ——
    这不是「吞异常」：两种格式都不成立时返回 ``None``，
    由 :func:`import_rules` 给出明确的报错文案
    （「既不是合法 JSON，也不是合法 YAML。」）。

    :param text: 文本内容。
    :return: 解析结果；两种格式都不合法时返回 ``None``。
    """
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        logger.debug("规则文本不是合法 JSON，改按 YAML 解析。")
        return _parse_yaml_text(text)
