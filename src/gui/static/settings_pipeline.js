/* 哨兵 Sentinel —— 设置子页面：审查流水线控制 + 呈现与摘要（v3.7.0）
 *
 * 这一页管两件事：
 *   ① 「这次到底谁来审？」—— L1 / L2 / L3 独立开关、L2→L3 桥接、
 *      L1 命中后的拦截策略。
 *   ② 「看板怎么显示？」—— 右侧日志与中间流水的两个极限压缩档位。
 *
 * ⚠️ 只写**本页面拥有**的键 —— 不碰别的页面的字段，
 *    免得「保存并应用」把隔壁页面的配置顶掉。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;

  /* 与 ``src/rules/registry.py`` 的 L1_ACTION_* 一一对应。 */
  var L1_ACTIONS = [
    "red_line_only",
    "suspend",
    "alert",
    "escalate"
  ];

  var L1_ACTION_LABELS = {
    red_line_only: "只拦红线级（默认）",
    suspend: "直接挂起",
    alert: "只告警不挂起",
    escalate: "放行给 L2 二次判断"
  };

  /* 两个档位的分段（v3.7.0）——
   * ⚠️ 看板那边的 ``logScaleMode`` / ``auditCompressionMode``
   *    用的是**同一套边界**，改一处必须改两处。 */
  var LOG_SCALE_BANDS = [
    { max: 30, name: "纯代码" },
    { max: 69, name: "自然语言" },
    { max: 99, name: "高度摘要" },
    { max: 100, name: "极限摘要" }
  ];

  var AUDIT_SCALE_BANDS = [
    { max: 39, name: "完整" },
    { max: 69, name: "精简" },
    { max: 89, name: "结论" },
    { max: 100, name: "极限" }
  ];

  var DEFAULT_LOG_COMPRESSION = 40;
  var DEFAULT_AUDIT_COMPRESSION = 60;

  /* --- v3.8.0：L3 回退预算的默认值（与后端一致）--- */
  var DEFAULT_L3_FALLBACK_LIMIT = 100;
  var DEFAULT_L3_FALLBACK_LIMIT_SECONDS = 3600;

  /* -------------------------------------------------------- 小工具 */

  /**
   * 把一个 0-100 的档位夹进合法区间。
   *
   * @param {*} raw 原始值。
   * @param {number} fallback 读不出来时的兜底。
   * @return {number} 合法档位。
   */
  function clampLevel(raw, fallback) {
    var parsed = Number(raw);
    if (!isFinite(parsed)) {
      return fallback;
    }
    return Math.min(100, Math.max(0, Math.round(parsed)));
  }

  /**
   * 档位落在哪一段。
   *
   * @param {number} level 档位（0-100）。
   * @param {Array<{max: number, name: string}>} bands 分段表。
   * @return {string} 分段名。
   */
  function bandName(level, bands) {
    var value = clampLevel(level, 0);
    for (var i = 0; i < bands.length; i += 1) {
      if (value <= bands[i].max) {
        return bands[i].name;
      }
    }
    return bands[bands.length - 1].name;
  }

  /**
   * 把档位写成「数值 · 分段名」。
   *
   * @param {number} level 档位。
   * @param {Array<{max: number, name: string}>} bands 分段表。
   * @return {string} 展示文本。
   */
  function levelText(level, bands) {
    return clampLevel(level, 0) + " · " + bandName(level, bands);
  }

  /* -------------------------------------------------------- 表单读取 */

  function collectPayload() {
    return {
      rules: {
        /* L1 总开关 —— 与「L1 规则」页是同一个配置键。 */
        enabled: C.checked("pipeline-l1-enabled"),
        /* L1 命中后的动作 —— 与「L1 规则」页是同一个配置键。 */
        l1_action: C.value("pipeline-l1-action")
      },
      cascade: {
        /* 级联总开关 —— v3.7.0 起从「模型路由」页搬到这里，
         * 因为它是「审不审」的决定，不是「用哪个模型」的决定。 */
        enabled: C.checked("pipeline-cascade-enabled"),
        l2: {
          enabled: C.checked("pipeline-l2-enabled")
        },
        l3: {
          enabled: C.checked("pipeline-l3-enabled")
        },
        bridge_l1_to_l3: C.checked("pipeline-bridge-l1-to-l3"),
        /* --- v3.8.0：L2 掉线热切 L3 + 回退预算熔断 --- */
        failover_to_l3: C.checked("pipeline-failover-to-l3"),
        l3_fallback_limit: Math.max(
          0,
          Math.round(C.num("pipeline-l3-fallback-limit"))
        ),
        l3_fallback_limit_seconds: Math.max(
          0,
          Math.round(C.num("pipeline-l3-fallback-limit-seconds"))
        ),
        l3_fallback_cost_limit: Math.max(
          0,
          C.num("pipeline-l3-fallback-cost-limit")
        ),
        l3_fallback_unit_cost: Math.max(
          0,
          C.num("pipeline-l3-fallback-unit-cost")
        )
      },
      presentation: {
        log_compression: clampLevel(
          C.num("presentation-log-compression"),
          DEFAULT_LOG_COMPRESSION
        ),
        audit_compression: clampLevel(
          C.num("presentation-audit-compression"),
          DEFAULT_AUDIT_COMPRESSION
        )
      }
    };
  }

  /* -------------------------------------------------------- 回填表单 */

  function fillForm(data) {
    var rules = data.rules || {};
    var cascade = data.cascade || {};
    var l2 = cascade.l2 || {};
    var l3 = cascade.l3 || {};
    var presentation = data.presentation || {};

    C.setChecked("pipeline-l1-enabled", rules.enabled);
    C.setChecked("pipeline-cascade-enabled", cascade.enabled);
    C.setChecked("pipeline-l2-enabled", l2.enabled);
    C.setChecked("pipeline-l3-enabled", l3.enabled);
    C.setChecked(
      "pipeline-bridge-l1-to-l3",
      cascade.bridge_l1_to_l3
    );

    /* --- v3.8.0：热切与预算 --- */
    C.setChecked(
      "pipeline-failover-to-l3",
      cascade.failover_to_l3 !== false
    );
    C.setValue(
      "pipeline-l3-fallback-limit",
      C.numOr(cascade.l3_fallback_limit, DEFAULT_L3_FALLBACK_LIMIT)
    );
    C.setValue(
      "pipeline-l3-fallback-limit-seconds",
      C.numOr(
        cascade.l3_fallback_limit_seconds,
        DEFAULT_L3_FALLBACK_LIMIT_SECONDS
      )
    );
    C.setValue(
      "pipeline-l3-fallback-cost-limit",
      C.numOr(cascade.l3_fallback_cost_limit, 0)
    );
    C.setValue(
      "pipeline-l3-fallback-unit-cost",
      C.numOr(cascade.l3_fallback_unit_cost, 0)
    );

    var action = String(rules.l1_action || "red_line_only");
    if (L1_ACTIONS.indexOf(action) < 0) {
      action = "red_line_only";
    }
    C.setValue("pipeline-l1-action", action);

    var logLevel = clampLevel(
      presentation.log_compression,
      DEFAULT_LOG_COMPRESSION
    );
    var auditLevel = clampLevel(
      presentation.audit_compression,
      DEFAULT_AUDIT_COMPRESSION
    );
    C.setValue("presentation-log-compression", logLevel);
    C.setValue("presentation-audit-compression", auditLevel);
    updateLevelLabels(logLevel, auditLevel);
    updatePath();
  }

  /* ------------------------------------------------------- 档位标签 */

  function updateLevelLabels(logLevel, auditLevel) {
    C.setText(
      "presentation-log-compression-value",
      levelText(logLevel, LOG_SCALE_BANDS)
    );
    C.setText(
      "presentation-audit-compression-value",
      levelText(auditLevel, AUDIT_SCALE_BANDS)
    );
  }

  /* -------------------------------------------------------- 通路摘要 */

  /**
   * 把四个开关拼成一句**人话**通路说明。
   *
   * ⚠️ 这段话是**如实**的 —— 桥接是单盲、只产标准级告警、
   *    永不挂起，这些限制必须写出来，不能让人误以为
   *    「关掉 L2」只是省显存那么简单。
   *
   * @param {object} state 当前开关状态。
   * @return {string} 通路说明。
   */
  function describePath(state) {
    if (!state.l1) {
      return "L1 已关闭 —— 本地正则完全不参与判定，" +
        "所有事件都不会被本地规则拦下。";
    }
    if (!state.cascade) {
      return "L1 开 · 级联总开关关闭 —— 只有 L1 正则在把关，" +
        "没有 L2 / L3 模型复核。";
    }
    if (!state.l2 && !state.l3) {
      return "L1 开 · L2 与 L3 都关着 —— 级联不启用，" +
        "只有 L1 正则在把关。";
    }
    if (!state.l2 && state.l3) {
      if (state.bridge) {
        return "L1 开 → L3 云端单盲（桥接已开）—— " +
          "L1 未命中的事件直接交给云端 L3 复核；" +
          "因为只剩 L3 一票，L3 说有问题也只出标准级告警，" +
          "不会挂起、不会亮最高级红灯。";
      }
      return "L1 开 → L3 云端（桥接未开）—— " +
        "L1 命中的事件由 L3 复核，但 L1 没命中的事件" +
        "没有第二道模型复核，只剩 L1 正则说了算。";
    }
    if (state.l2 && !state.l3) {
      return "L1 开 → L2 本地过滤 —— 云端终审已关，" +
        "L2 判「正常」的事件被静默丢弃，不会再上报云端。";
    }
    var tail = state.failover
      ? " L2 临时掉线时会自动把载荷转给 L3（单盲，不亮最高级红灯）。"
      : " L2 掉线时直接回退 L1 纯规则（热切已关闭）。";
    return "L1 开 → L2 本地过滤 → L3 云端终审（标准双盲）—— " +
      "L2 判「正常」就丢弃，L2 说有问题才叫醒 L3。" + tail;
  }

  /**
   * 刷新「当前生效通路」那一行。
   *
   * @return {void}
   */
  function updatePath() {
    var state = {
      l1: C.checked("pipeline-l1-enabled"),
      cascade: C.checked("pipeline-cascade-enabled"),
      l2: C.checked("pipeline-l2-enabled"),
      l3: C.checked("pipeline-l3-enabled"),
      bridge: C.checked("pipeline-bridge-l1-to-l3"),
      /* v3.8.0：热切开关。 */
      failover: C.checked("pipeline-failover-to-l3")
    };
    C.setText("pipeline-path", describePath(state));

    /* 桥接的**前置条件**不满足时，把话说清楚 ——
     * 否则用户勾了桥接却一点动静都没有。 */
    var note = document.getElementById("pipeline-path-note");
    if (!note) {
      return;
    }
    if (state.bridge && !(state.l2 === false && state.l3 === true)) {
      note.textContent =
        "⚠️ 桥接当前**不会生效** —— 它要求「L2 关闭 + L3 开启」。" +
        "现在这两个条件没同时满足，勾上也没用。";
      note.className = "settings-note settings-note--warn";
      return;
    }
    if (state.failover && !state.l3) {
      note.textContent =
        "⚠️ 热切当前**不会生效** —— L3 云端终审已关闭，" +
        "没有第二个人可以接手 L2 的活。";
      note.className = "settings-note settings-note--warn";
      return;
    }
    note.textContent =
      "开关改完点下面的「保存并应用」，编排器会立刻重建 —— " +
      "不必重启哨兵。";
    note.className = "settings-note";
  }

  /* ------------------------------------------------------------ 启动 */

  function init() {
    C.bindCommonActions(collectPayload, fillForm);

    var logSlider = document.getElementById("presentation-log-compression");
    if (logSlider) {
      var onLog = function () {
        updateLevelLabels(
          clampLevel(logSlider.value, DEFAULT_LOG_COMPRESSION),
          clampLevel(
            C.num("presentation-audit-compression"),
            DEFAULT_AUDIT_COMPRESSION
          )
        );
      };
      logSlider.addEventListener("input", onLog);
      logSlider.addEventListener("change", onLog);
    }

    var auditSlider = document.getElementById(
      "presentation-audit-compression"
    );
    if (auditSlider) {
      var onAudit = function () {
        updateLevelLabels(
          clampLevel(
            C.num("presentation-log-compression"),
            DEFAULT_LOG_COMPRESSION
          ),
          clampLevel(auditSlider.value, DEFAULT_AUDIT_COMPRESSION)
        );
      };
      auditSlider.addEventListener("input", onAudit);
      auditSlider.addEventListener("change", onAudit);
    }

    /* 四个开关任一变化 → 立刻刷新通路说明（不用等保存）。 */
    var ids = [
      "pipeline-l1-enabled",
      "pipeline-cascade-enabled",
      "pipeline-l2-enabled",
      "pipeline-l3-enabled",
      "pipeline-bridge-l1-to-l3",
      /* v3.8.0：热切开关也参与通路说明。 */
      "pipeline-failover-to-l3"
    ];
    ids.forEach(function (id) {
      var node = document.getElementById(id);
      if (node) {
        node.addEventListener("change", updatePath);
      }
    });

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
