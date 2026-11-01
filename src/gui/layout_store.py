"""左右分栏比例的记忆。

来源：旧项目「个人项目：agent中心」``gui/layout_store.py``。
该模块**零业务依赖**（不 import 任何 ``core.*``），是旧项目里少数
可以直接搬走的部分。

为什么放后端而不是 localStorage
================================
服务端在构建页面的那一刻就知道比例 —— ``ui.splitter(value=…)`` 直接就是对的，
不用「先按默认渲染、再用 JS 改回去」那种会**闪一下**的做法；
而且后端方案**能写单测**，也不依赖浏览器的存储权限
（隐私模式 / 策略禁用下 localStorage 会直接抛异常）。

铁律
====

* 写文件**必须原子**：先写同目录的临时文件，再 ``os.replace`` 覆盖。
  写到一半断电会留个半截 JSON，下次启动整个面板读不出来 —— 那比不记忆还糟。
* **读失败一律回落到默认值**，绝不因为一个坏文件让整个面板 500。
* 文件只落在**项目目录内**，不写用户目录、不写系统目录。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "DEFAULT_PATH",
    "DEFAULT_RATIO",
    "MAX_RATIO",
    "MIN_RATIO",
    "clamp_ratio",
    "load_ratio",
    "reset_layout",
    "save_ratio",
]

#: 初始比例：左（会话 / 状态栏）30% / 右（事件流）70%。
DEFAULT_RATIO = 30.0

#: 拖拽范围。太小左栏就没法看卡片，太大右栏就没法看事件流。
MIN_RATIO = 15.0
MAX_RATIO = 60.0

#: 布局文件（项目目录内）。
DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / ".sentinel_layout.json"

#: 文件里存的东西就一个键。别往里塞别的东西。
_KEY = "splitter_ratio"


def clamp_ratio(value: Any) -> float:
    """把任意输入夹到合法区间。

    ⛔ 夹不到合法值就返回默认值 —— 宁可回到 3:7，也不要一个离谱的比例
    （比如 0 会让左栏彻底消失，100 会让右栏消失）。

    :param value: 任意输入。
    :return: ``MIN_RATIO`` .. ``MAX_RATIO`` 之间的浮点数。
    """
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RATIO
    if math.isnan(ratio):
        return DEFAULT_RATIO
    return max(MIN_RATIO, min(MAX_RATIO, ratio))


def load_ratio(path: Optional[Path] = None) -> float:
    """读回拖出来的比例。读不到就用默认值。

    ⛔ **绝不抛异常**：布局文件坏了也不能让整个面板起不来。

    :param path: 布局文件路径；为 ``None`` 时用默认路径。
    :return: 分栏比例。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return DEFAULT_RATIO
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return DEFAULT_RATIO
    if not isinstance(data, dict):
        return DEFAULT_RATIO
    return clamp_ratio(data.get(_KEY, DEFAULT_RATIO))


def save_ratio(value: Any, path: Optional[Path] = None) -> bool:
    """记下拖出来的比例。返回是否真的写成功。

    写失败（只读目录 / 磁盘满）**不能打断交互** —— 拖拽照常能用，
    只是这次不记住。

    :param value: 比例（会被夹到合法区间）。
    :param path:  布局文件路径；为 ``None`` 时用默认路径。
    :return: 是否写成功。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    ratio = clamp_ratio(value)
    payload = json.dumps({_KEY: ratio}, ensure_ascii=False, indent=2)
    tmp_name: Optional[str] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # ⭐ 原子写：同目录临时文件 → os.replace 覆盖。
        #    直接 open(target,'w') 的话，写到一半崩了会留半截 JSON。
        #    mkstemp 保证临时名唯一 —— 同一个进程里连着存两次也不会撞。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except (OSError, ValueError):
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
        return False
    return True


def reset_layout(path: Optional[Path] = None) -> bool:
    """删掉布局文件，回到 3 : 7。

    :param path: 布局文件路径；为 ``None`` 时用默认路径。
    :return: 是否删除成功（文件本来就不存在也算成功）。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return True
