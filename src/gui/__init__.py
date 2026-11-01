"""哨兵 Sentinel —— 图形界面层。

本目录包含**与业务无关的持久化工具**：

* :mod:`src.gui.layout_store` —— 左右分栏比例的记忆（原子写 + 失败回落）；
* :mod:`src.gui.density_store` —— 信息密度档位的记忆（原子写 + 失败回落）；
* :mod:`src.gui.density` —— 密度档位定义与夹取。

来源：旧项目「个人项目：agent中心」``gui/layout_store.py`` 与
``gui/density_store.py``。这两个模块**零业务依赖**（不 import 任何
``core.*``），是旧项目里少数可以直接搬走的部分，已在
``_管理文档/文档与报告/versions/v0.3.0-asset-audit.md`` 第 6.1 节记录。

⚠️ **本阶段不引入 nicegui 依赖。** 哨兵目前是终端优先工具；
旧项目的 ``gui/app.py``（4,115 行单页巨物）经评估**不可移植**。
真正做 GUI 时，这里放新写的代码，不搬旧项目的页面。

监控面板
========
:mod:`src.gui.server` 是新写的本地只读监控面板（FastAPI + Jinja2 + SSE），
入口 ``python -m src.gui.server --watch <日志>``。它**不 import**
``src.core.engine`` —— 扫描能力通过 ``scan_callback`` 注入，
GUI 与引擎之间只有「一个告警日志文件」这一条耦合。

为避免没装 fastapi 时整个 ``src.gui`` 包导入失败（``src.gui.density`` 等
无依赖模块仍要能用），这里用 :pep:`562` 的模块级 ``__getattr__` 做惰性导出。
"""

from typing import Any

__all__ = ["create_app", "main"]


def __getattr__(name: str) -> Any:
    """惰性导出监控面板入口。

    只有真正访问 ``src.gui.create_app`` / ``src.gui.main`` 时才会导入
    :mod:`src.gui.server`，从而把 fastapi 变成可选依赖。

    :param name: 被访问的属性名。
    :return: 对应的入口对象。
    :raises AttributeError: 属性名不认识时抛出。
    """
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
