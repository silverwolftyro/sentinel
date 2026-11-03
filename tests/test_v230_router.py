"""v2.3.0 回归：Web UI 路由化（独立设置页）。

背景
====

v2.2.x 之前哨兵只有**一个**页面，功能全堆在上面：
顶栏一堆按钮、弹窗、模态框……用户抱怨「界面元素堆砌过多」。

v2.3.0 引入**前端路由**：

* ``/`` —— 主看板；
* ``/settings`` —— **独立全屏**的设置页
  （不是抽屉、不是弹窗、不是模态框）。

本文件钉住：

1. ``/settings`` 有**自己的 URL**，直接访问能用（刷新不丢）；
2. 顶栏有【⚙ 设置】入口，且它是**带 data-nav 的站内链接**
   （前端路由器靠这个属性接管）；
3. 设置页要能通过 API **读**配置、**写回**配置，并触发热重载；
4. 密钥**打码**下发、**打码值不会被写回去**（不能把真密钥抹成 ****）；
5. 配置写回是**外科手术式的** —— 注释必须保留。
"""

from __future__ import annotations

CONFIG_SAMPLE = """# 哨兵配置（测试用）
llm:
  # 主力模型
  provider: "ollama"
  base_url: "http://localhost:11434"
  model: "qwen2.5:7b"
  timeout: 20
  # 这行注释在保存后**必须**还在
  risk_threshold: 0.7
  api_key: "sk-真的密钥"

rules:
  enabled: true
  # 高危命令正则（增量追加到内置表之后）
  dangerous_commands:
    - '\\brm\\s+-rf\\b'
    - '\\bsudo\\b'
  hot_reload:
    enabled: true
    interval: 1.0

alert:
  enabled: true
  channels:
    - type: "terminal"
      enabled: false
    - type: "webhook"
      enabled: true
      url: "https://example.invalid/hook"
      format: "generic"
      secret: "wh-真的密钥"
"""
