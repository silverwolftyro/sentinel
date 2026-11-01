"""信息密度档位的记忆。

来源：旧项目「个人项目：agent中心」``gui/density_store.py``。
与 :mod:`src.gui.layout_store` 同源，零业务依赖。

为什么单独一个文件
==================

布局记忆在 ``.sentinel_layout.json``，密度记忆另开一个 ——
两件事互不干扰，清一个不会连累另一个。

语义
====

* ``save_level()`` —— 勾了「记住我的选择」时调用。
* ``clear_level()`` —— **取消勾选**时调用：删掉记忆，
  下次开面板回到默认档位（最详细）。
* ``load_level()`` —— 开面板时读；读不到就用默认档位。

铁律
====

* **写文件必须原子**（同目录临时文件 → ``os.replace``）。
  写到一半断电会留个半截 JSON，下次启动整个面板读不出来。
* **读失败一律回落到默认值**，绝不因为一个坏文件让面板 500。
* 回落的默认值是**最详细**那档 —— 宁可多给，不可少给。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from .density import DENSITY_MIN, clamp_level

__all__ = [
    "DEFAULT_PATH",
    "clear_level",
    "has_level",
    "load_level",
    "save_level",
]

#: 密度记忆文件（项目目录内）。
DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent.parent / ".sentinel_density.json"
)

#: 文件里存的东西就一个键。别往里塞别的东西。
_KEY = "density_level"


def load_level(path: Optional[Path] = None) -> int:
    """读回记住的档位。读不到就用默认值（最详细）。

    ⛔ **绝不抛异常**：记忆文件坏了也不能让整个面板起不来。

    :param path: 文件路径；为 ``None`` 时用默认路径。
    :return: 密度档位。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return DENSITY_MIN
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return DENSITY_MIN
    if not isinstance(data, dict):
        return DENSITY_MIN
    return clamp_level(data.get(_KEY, DENSITY_MIN))


def has_level(path: Optional[Path] = None) -> bool:
    """是不是**勾过**「记住我的选择」？

    用来在开面板时决定复选框该是勾上还是没勾 ——
    没有记忆文件 = 没勾过 = 只在当前会话生效。

    :param path: 文件路径；为 ``None`` 时用默认路径。
    :return: 是否存在记忆文件。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    return target.is_file()


def save_level(value: Any, path: Optional[Path] = None) -> bool:
    """记下选中的档位。返回是否真的写成功。

    写失败（只读目录 / 磁盘满）**不能打断交互** —— 滑条照常能用，
    只是这次不记住。

    :param value: 档位（会被夹到合法区间）。
    :param path:  文件路径；为 ``None`` 时用默认路径。
    :return: 是否写成功。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    payload = json.dumps({_KEY: clamp_level(value)}, ensure_ascii=False, indent=2)
    tmp_name: Optional[str] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
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


def clear_level(path: Optional[Path] = None) -> bool:
    """删掉密度记忆，回到默认档位（取消「记住我的选择」时调用）。

    :param path: 文件路径；为 ``None`` 时用默认路径。
    :return: 是否删除成功（文件本来就不存在也算成功）。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return True
