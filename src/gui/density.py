"""信息密度（精简度）档位定义。

⚠️ **本模块只管档位定义，不做裁剪。**

旧项目的 ``gui/density.py`` 里还有 ``apply_density()``（真正裁剪对话的
纯函数），但它依赖 ``core.chat_view`` 的 ``ChatTurn`` / ``ChatBlock`` /
``BlockKind`` —— 那是旧项目的聊天模型，哨兵没有对话，**故不移植**。

这里只保留**与数据模型无关**的部分：档位常量、档位名字、档位说明、
以及夹取函数。将来做 GUI 时，裁剪逻辑要针对哨兵自己的事件模型重写，
但这套「档位语义」可以沿用。

档位语义（沿用旧项目）
======================

===== ============== ==================================================
档位  名字          干了什么
===== ============== ==================================================
0     原始全文      一字不改，思考链 / 工具调用 / 系统提示全留
1     轻微精简      长段落截断 600 字，其余不动
2     中度精简      段落 300 / 思考 200 / 工具参数 80 字
3     高度精简      只留**问答**（各 160 字），其余收起
4     只看结论      只留助手回复（120 字），提问也收起
===== ============== ==================================================

⛔ **越往右越少** —— 与直觉一致：滑条拖到右边 = 更精简。

铁律
====

* **只影响展示**：被裁掉的原文一个字节都不丢，裁剪结果是**新的**结构；
* **如实标注**：截断过的文本必须写明「原文多少字」，
  不许让人以为那就是全部；
* **不许自作主张**：任何「这条重要那条不重要」的判断都不在这里。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DENSITY_HINTS",
    "DENSITY_LABELS",
    "DENSITY_MAX",
    "DENSITY_MIN",
    "clamp_level",
    "density_hint",
    "density_label",
]

#: 滑条最左（最详细）。
DENSITY_MIN = 0
#: 滑条最右（最精简）。
DENSITY_MAX = 4

#: 每一档的短名字（滑条刻度）。
DENSITY_LABELS: tuple[str, ...] = (
    "原始全文",
    "轻微精简",
    "中度精简",
    "高度精简",
    "只看结论",
)

#: 每一档给用户看的一句话解释。
DENSITY_HINTS: tuple[str, ...] = (
    "一字不改：思考过程、工具调用、系统提示全部保留。",
    "长段落截断到 600 字，思考链与工具调用仍完整可见。",
    "段落截断到 300 字，思考链缩到 200 字，工具参数缩到 80 字。",
    "只保留「您说了什么 / 它答了什么」（各 160 字），"
    "思考链、工具调用、系统提示全部收起。",
    "每轮只留助手的回复（120 字），提问与思考链一律不显示。",
)


def clamp_level(value: Any) -> int:
    """把任意输入夹到合法档位。

    ⛔ 夹不到合法值就回落到**最详细**那档 —— 宁可多给，不可少给。
    信息能不能看到，必须由用户决定，不能由一个坏文件替他决定。

    :param value: 任意输入。
    :return: ``DENSITY_MIN`` .. ``DENSITY_MAX`` 之间的整数。
    """
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DENSITY_MIN
    return max(DENSITY_MIN, min(DENSITY_MAX, level))


def density_label(level: Any) -> str:
    """档位 → 短名字。"""
    return DENSITY_LABELS[clamp_level(level)]


def density_hint(level: Any) -> str:
    """档位 → 一句话解释。"""
    return DENSITY_HINTS[clamp_level(level)]
