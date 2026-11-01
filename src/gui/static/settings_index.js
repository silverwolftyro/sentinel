/* 哨兵 Sentinel —— 设置**主页**（/settings）（v3.0.2）
 *
 * 这一页**只有**一个菜单列表。
 *
 * ⚠️ 它不收集任何配置、不保存任何东西 ——
 * 点卡片跳到那个功能**自己的页面**，由那个页面负责读写。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;

  function setSummary(id, text) {
    C.setText(id, text);
  }

  function fillForm(data) {
    var llm = data.llm || {};
    var routing = data.routing || {};
    var fallback = routing.fallback || {};
    var rules = data.rules || {};
    var channels = Array.isArray(data.channels) ? data.channels : [];
    var sessions = Array.isArray(data.sessions) ? data.sessions : [];
    var cascade = data.cascade || {};
    var l2 = cascade.l2 || {};
    var l3 = cascade.l3 || {};

    setSummary(
      "menu-llm-summary",
      "当前：" + (llm.provider || "（未配置）") +
      " / " + (llm.model || "–") +
      "　降级：" + (fallback.model || "–") +
      "　多级路由：" + (routing.enabled ? "开" : "关")
    );

    /* v3.7.0：流水线一句话摘要 —— 与「流水线控制」页的口径一致。 */
    var pipeline = [];
    pipeline.push("L1 " + (rules.enabled ? "开" : "关"));
    pipeline.push("L2 " + (l2.enabled === false ? "关" : "开"));
    pipeline.push("L3 " + (l3.enabled === false ? "关" : "开"));
    if (cascade.bridge_l1_to_l3) {
      pipeline.push("桥接开");
    }
    setSummary("menu-pipeline-summary", pipeline.join("　·　"));

    var enabled = channels.filter(function (item) {
      return item && item.enabled;
    });
    setSummary(
      "menu-channels-summary",
      "共 " + channels.length + " 个通道，已启用 " + enabled.length + " 个"
    );

    var commands = Array.isArray(rules.dangerous_commands)
      ? rules.dangerous_commands.length
      : 0;
    setSummary(
      "menu-rules-summary",
      "L1 " + (rules.enabled ? "已启用" : "未启用") +
      "　自定义高危正则 " + commands + " 条"
    );

    setSummary(
      "menu-sessions-summary",
      "正在监控 " + sessions.length + " 路会话"
    );
  }

  /* ------------------------------------------------------------ 启动 */

  function init() {
    C.loadConfig().then(function (data) {
      if (data) {
        fillForm(data);
      }
    });
  }

  function destroy() {
    /* 本页没有定时器、没有 SSE —— DOM 被换掉就等于收摊了。 */
  }

  window.SentinelPage = { init: init, destroy: destroy };

  C.boot(init);
})();
