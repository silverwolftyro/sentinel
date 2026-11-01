/* 哨兵 Sentinel —— 设置子页面：L1 规则管理（v3.7.0）
 *
 * 这一页管两件事：
 *   1. **规则清单表格** —— 每条规则一个开关 + 命中动作；
 *   2. **全量导入 / 导出** —— JSON / YAML。
 *
 * ⚠️ 状态为什么自己留一份，不直接读 DOM
 * --------------------------------------
 * 规则表是**动态渲染**的（清单来自 ``/api/rules``），
 * 收集表单时 DOM 里的开关状态是可读的，但
 * 「关掉」与「在白名单之外」是**两回事** ——
 * 红线类别还受 ``rules.catastrophic.categories`` 白名单约束。
 * 所以这里显式维护三份集合：
 *   disabled  —— 被用户关掉的规则 id
 *   logOnly   —— 被降级成「仅记录」的规则 id
 *   categories—— 当前**开着**的红线类别
 * 保存时把它们原样写回去，规则表才是真正说了算的那一方。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;

  /* 规则分类 → 中文分组名（顺序即表格里的分组顺序）。 */
  var KIND_GROUPS = [
    { kind: "catastrophic", title: "红线级（catastrophic）· 守护者唯一触发源" },
    { kind: "dangerous", title: "高危命令（dangerous）· 命中即出告警" },
    { kind: "loop", title: "死循环（loop）· 连续重复读取同一文件" }
  ];

  /* 风险等级 → 徽章样式。 */
  var LEVEL_BADGES = {
    critical: { cls: "settings-badge--critical", text: "高危" },
    warn: { cls: "settings-badge--warn", text: "警告" },
    info: { cls: "", text: "提示" }
  };

  /* ------------------------------------------------------------ 状态 */

  var tableState = {
    rules: [],
    disabled: {},
    logOnly: {},
    categories: {},
    catastrophicEnabled: true,
    custom: []
  };

  /* ------------------------------------------------------- 小工具 */

  function byId(id) {
    return document.getElementById(id);
  }

  function isCatastrophic(rule) {
    return String(rule && rule.kind) === "catastrophic";
  }

  function categoryOf(rule) {
    return String((rule && rule.id) || "").split(".", 2)[1] || "";
  }

  /**
   * 这条规则当前**显示**为开还是关。
   *
   * ⚠️ 红线规则要三重条件全满足才是「开」：
   *    总开关开 + 没被关掉 + 在白名单里。
   *    少一条，页面上那个开关就必须是灰的 ——
   *    否则就变成了「看起来开着、其实不生效」的死控件。
   *
   * @param {object} rule 规则行。
   * @return {boolean} 显示为开返回 true。
   */
  function ruleEnabled(rule) {
    if (!rule) {
      return false;
    }
    if (isCatastrophic(rule) && !tableState.catastrophicEnabled) {
      return false;
    }
    if (tableState.disabled[rule.id]) {
      return false;
    }
    if (isCatastrophic(rule)) {
      return !!tableState.categories[categoryOf(rule)];
    }
    return true;
  }

  /**
   * 收集当前应当写回的「被关掉的规则 id」。
   *
   * @return {string[]} 规则 id 列表。
   */
  function collectDisabled() {
    var ids = [];
    Object.keys(tableState.disabled).forEach(function (id) {
      if (tableState.disabled[id]) {
        ids.push(id);
      }
    });
    return ids.sort();
  }

  /**
   * 收集当前应当写回的「仅记录」规则 id。
   *
   * ⚠️ 关掉的规则不写进去 —— 它根本不参与判定，
   *    留着「仅记录」标记只会让配置文件变得难以理解。
   *
   * @return {string[]} 规则 id 列表。
   */
  function collectLogOnly() {
    var ids = [];
    Object.keys(tableState.logOnly).forEach(function (id) {
      if (!tableState.logOnly[id]) {
        return;
      }
      for (var i = 0; i < tableState.rules.length; i += 1) {
        if (tableState.rules[i].id === id && ruleEnabled(tableState.rules[i])) {
          ids.push(id);
          break;
        }
      }
    });
    return ids.sort();
  }

  /**
   * 收集当前**开着**的红线类别。
   *
   * @return {string[]} 类别名列表。
   */
  function collectCategories() {
    var names = [];
    Object.keys(tableState.categories).forEach(function (name) {
      if (tableState.categories[name]) {
        names.push(name);
      }
    });
    return names.sort();
  }

  /* ------------------------------------------------------ 渲染表格 */

  /**
   * 造一个单元格里的小徽章。
   *
   * @param {string} text 徽章文字。
   * @param {string} cls  额外样式类。
   * @return {HTMLElement} 徽章元素。
   */
  function makeBadge(text, cls) {
    var span = document.createElement("span");
    span.className = "settings-badge" + (cls ? " " + cls : "");
    span.textContent = text;
    return span;
  }

  /**
   * 造一行规则。
   *
   * @param {object} rule 规则行。
   * @return {HTMLElement} ``<tr>`` 元素。
   */
  function buildRuleRow(rule) {
    var tr = document.createElement("tr");
    tr.setAttribute("data-rule-id", String(rule.id));

    var nameCell = document.createElement("td");
    var nameBox = document.createElement("span");
    nameBox.className = "settings-table__name";
    nameBox.textContent = String(rule.name || rule.id);
    nameCell.appendChild(nameBox);
    tr.appendChild(nameCell);

    var patternCell = document.createElement("td");
    var patternBox = document.createElement("code");
    patternBox.className = "settings-table__pattern";
    patternBox.textContent = String(rule.pattern || "");
    patternCell.appendChild(patternBox);
    tr.appendChild(patternCell);

    var levelCell = document.createElement("td");
    var level = LEVEL_BADGES[String(rule.level)] || LEVEL_BADGES.info;
    levelCell.appendChild(makeBadge(level.text, level.cls));
    if (String(rule.severity) === "red_line") {
      levelCell.appendChild(document.createTextNode(" "));
      levelCell.appendChild(makeBadge("红线", "settings-badge--redline"));
    }
    tr.appendChild(levelCell);

    var actionCell = document.createElement("td");
    var select = document.createElement("select");
    select.className = "settings-table__action";
    select.setAttribute("data-rule-id", String(rule.id));
    select.setAttribute("aria-label", String(rule.name || rule.id) + " 的命中动作");
    var options = [
      { value: "block", text: "拦截" },
      { value: "log", text: "仅记录" }
    ];
    options.forEach(function (option) {
      var node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.text;
      select.appendChild(node);
    });
    select.value = tableState.logOnly[rule.id] ? "log" : "block";
    actionCell.appendChild(select);
    tr.appendChild(actionCell);

    var toggleCell = document.createElement("td");
    var label = document.createElement("label");
    label.className = "settings-toggle";
    var box = document.createElement("input");
    box.type = "checkbox";
    box.setAttribute("data-rule-id", String(rule.id));
    box.setAttribute(
      "aria-label",
      "启用规则 " + String(rule.name || rule.id)
    );
    box.checked = ruleEnabled(rule);
    var track = document.createElement("span");
    track.className = "settings-toggle__track";
    label.appendChild(box);
    label.appendChild(track);
    toggleCell.appendChild(label);
    tr.appendChild(toggleCell);

    /* 关掉的规则：动作选择器一起禁用 —— 免得出现
       「规则已关，动作还选着仅记录」这种自相矛盾的状态。 */
    var active = ruleEnabled(rule);
    select.disabled = !active;
    box.disabled = isCatastrophic(rule) && !tableState.catastrophicEnabled;
    return tr;
  }

  /**
   * 重画整张规则表。
   *
   * @return {void}
   */
  function renderTable() {
    var body = byId("rules-table-body");
    if (!body) {
      return;
    }
    while (body.firstElementChild) {
      body.removeChild(body.firstElementChild);
    }
    if (!tableState.rules.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = "没有读到任何规则 —— 配置文件可能还没准备好。";
      empty.appendChild(cell);
      body.appendChild(empty);
      C.setText("rules-table-count", "0");
      return;
    }
    KIND_GROUPS.forEach(function (group) {
      var rows = tableState.rules.filter(function (rule) {
        return String(rule.kind) === group.kind;
      });
      if (!rows.length) {
        return;
      }
      var head = document.createElement("tr");
      head.className = "settings-table__group";
      var headCell = document.createElement("td");
      headCell.colSpan = 5;
      headCell.textContent = group.title + "（" + rows.length + " 条）";
      head.appendChild(headCell);
      body.appendChild(head);
      rows.forEach(function (rule) {
        body.appendChild(buildRuleRow(rule));
      });
    });
    C.setText("rules-table-count", String(tableState.rules.length));
  }

  /* ------------------------------------------------------ 交互绑定 */

  /**
   * 打开 / 关闭一条规则。
   *
   * @param {string} ruleId 规则 id。
   * @param {boolean} on    是否打开。
   * @return {void}
   */
  function setRuleEnabled(ruleId, on) {
    var rule = null;
    for (var i = 0; i < tableState.rules.length; i += 1) {
      if (tableState.rules[i].id === ruleId) {
        rule = tableState.rules[i];
        break;
      }
    }
    if (!rule) {
      return;
    }
    if (on) {
      delete tableState.disabled[ruleId];
      if (isCatastrophic(rule)) {
        tableState.categories[categoryOf(rule)] = true;
      }
    } else {
      tableState.disabled[ruleId] = true;
      if (isCatastrophic(rule)) {
        delete tableState.categories[categoryOf(rule)];
      }
    }
    renderTable();
  }

  /**
   * 某条规则的命中动作改了。
   *
   * @param {string} ruleId 规则 id。
   * @param {string} action  ``block`` 或 ``log``。
   * @return {void}
   */
  function setRuleAction(ruleId, action) {
    if (action === "log") {
      tableState.logOnly[ruleId] = true;
    } else {
      delete tableState.logOnly[ruleId];
    }
  }

  /**
   * 把 ``/api/rules`` 的快照灌进状态并重画表格。
   *
   * @param {object} snapshot 规则清单快照。
   * @return {void}
   */
  function applySnapshot(snapshot) {
    var data = snapshot || {};
    tableState.rules = Array.isArray(data.rules) ? data.rules : [];
    tableState.disabled = {};
    (data.disabled || []).forEach(function (id) {
      tableState.disabled[id] = true;
    });
    tableState.logOnly = {};
    (data.log_only || []).forEach(function (id) {
      tableState.logOnly[id] = true;
    });
    tableState.categories = {};
    (data.catastrophic_categories || []).forEach(function (name) {
      tableState.categories[name] = true;
    });
    tableState.catastrophicEnabled = data.catastrophic_enabled !== false;
    tableState.custom = Array.isArray(data.custom) ? data.custom : [];
    C.setChecked("rules-catastrophic-enabled", tableState.catastrophicEnabled);
    renderTable();
  }

  /**
   * 读取规则清单。
   *
   * @return {Promise} 完成即 resolve。
   */
  function loadRules() {
    return fetch("/api/rules")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          applySnapshot(data);
        } else {
          C.showResult(
            byId("rules-import-result"),
            "error",
            "读取规则清单失败：" + ((data && data.error) || "未知错误")
          );
        }
        return data;
      })
      .catch(function (err) {
        C.showResult(
          byId("rules-import-result"),
          "error",
          "读取规则清单失败：" + err.message
        );
        return null;
      });
  }

  /* --------------------------------------------------- 导入 / 导出 */

  /**
   * 导出全部规则并填进文本框。
   *
   * @param {string} format ``json`` 或 ``yaml``。
   * @return {Promise} 完成即 resolve。
   */
  function exportRules(format) {
    return fetch("/api/rules/export?format=" + encodeURIComponent(format))
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          C.showResult(
            byId("rules-import-result"),
            "error",
            "导出失败：" + ((data && data.error) || "未知错误")
          );
          return null;
        }
        C.setValue("rules-export-text", data.text || "");
        C.showResult(
          byId("rules-import-result"),
          "ok",
          "已导出 " + data.filename + "（" + (data.format || format) + "）。"
        );
        return data;
      })
      .catch(function (err) {
        C.showResult(
          byId("rules-import-result"),
          "error",
          "导出失败：" + err.message
        );
        return null;
      });
  }

  /**
   * 导入规则并热重载。
   *
   * @return {Promise} 完成即 resolve。
   */
  function importRules() {
    var text = C.value("rules-import-text");
    if (!text.trim()) {
      C.showResult(
        byId("rules-import-result"),
        "error",
        "没有要导入的内容 —— 先把 JSON 或 YAML 粘进导入框。"
      );
      return Promise.resolve(null);
    }
    C.showResult(byId("rules-import-result"), "wait", "正在导入…");
    return fetch("/api/rules/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          var errors = (data && data.errors) || [];
          C.showResult(
            byId("rules-import-result"),
            "error",
            "导入失败：" +
              ((data && data.error) || "未知错误") +
              (errors.length ? "（" + errors.join("；") + "）" : "")
          );
          return null;
        }
        applySnapshot(data);
        var note = data.message || "导入完成。";
        if (data.reloaded) {
          note += " 规则已热重载。";
        } else if (data.reload_note) {
          note += " " + data.reload_note;
        }
        C.showResult(byId("rules-import-result"), "ok", note);
        return data;
      })
      .catch(function (err) {
        C.showResult(
          byId("rules-import-result"),
          "error",
          "导入失败：" + err.message
        );
        return null;
      });
  }

  /* ------------------------------------------------------ 表单读写 */

  /**
   * 收集本页要保存的配置。
   *
   * @return {object} ``POST /api/config`` 的载荷。
   */
  function collectPayload() {
    return {
      rules: {
        enabled: C.checked("rules-enabled"),
        dangerous_commands: C.splitLines("rules-dangerous-commands"),
        hot_reload_enabled: C.checked("rules-hot-reload-enabled"),
        catastrophic_enabled: C.checked("rules-catastrophic-enabled"),
        catastrophic_categories: collectCategories(),
        l1_action: C.value("rules-l1-action"),
        disabled_rules: collectDisabled(),
        log_only_rules: collectLogOnly(),
        custom_rules: tableState.custom
      }
    };
  }

  /**
   * 用服务端返回的配置回填表单。
   *
   * @param {object} data ``/api/config`` 的返回。
   * @return {void}
   */
  function fillForm(data) {
    var rules = (data && data.rules) || {};
    var hotReload = rules.hot_reload || {};
    var catastrophic = rules.catastrophic || {};

    C.setChecked("rules-enabled", rules.enabled);
    C.setChecked("rules-hot-reload-enabled", hotReload.enabled);
    C.setText(
      "rules-hot-reload-interval",
      hotReload.interval === undefined || hotReload.interval === null
        ? "–"
        : String(hotReload.interval)
    );
    C.setValue(
      "rules-dangerous-commands",
      Array.isArray(rules.dangerous_commands)
        ? rules.dangerous_commands.join("\n")
        : ""
    );
    C.setValue("rules-l1-action", String(rules.l1_action || "red_line_only"));
    C.setChecked("rules-catastrophic-enabled", catastrophic.enabled !== false);
    loadRules();
  }

  /* ------------------------------------------------------------ 启动 */

  function init() {
    C.bindCommonActions(collectPayload, fillForm);

    var body = byId("rules-table-body");
    if (body) {
      body.addEventListener("change", function (event) {
        var node = event.target;
        if (!node || !node.getAttribute) {
          return;
        }
        var ruleId = node.getAttribute("data-rule-id");
        if (!ruleId) {
          return;
        }
        if (node.tagName === "INPUT" && node.type === "checkbox") {
          setRuleEnabled(ruleId, node.checked);
        } else if (node.tagName === "SELECT") {
          setRuleAction(ruleId, node.value);
        }
      });
    }

    var master = byId("rules-catastrophic-enabled");
    if (master) {
      master.addEventListener("change", function () {
        tableState.catastrophicEnabled = master.checked;
        renderTable();
      });
    }

    var exportJson = byId("rules-export-json");
    if (exportJson) {
      exportJson.addEventListener("click", function () {
        exportRules("json");
      });
    }
    var exportYaml = byId("rules-export-yaml");
    if (exportYaml) {
      exportYaml.addEventListener("click", function () {
        exportRules("yaml");
      });
    }
    var copyButton = byId("rules-copy-export");
    if (copyButton) {
      copyButton.addEventListener("click", function () {
        var text = C.value("rules-export-text");
        if (!text) {
          C.showResult(
            byId("rules-import-result"),
            "error",
            "还没有导出内容 —— 先点「导出 JSON」。"
          );
          return;
        }
        C.copyText(text).then(function () {
          C.showResult(
            byId("rules-import-result"),
            "ok",
            "已复制到剪贴板。"
          );
        }).catch(function (err) {
          C.showResult(
            byId("rules-import-result"),
            "error",
            "复制失败：" + err.message
          );
        });
      });
    }
    var importButton = byId("rules-import-button");
    if (importButton) {
      importButton.addEventListener("click", function () {
        importRules();
      });
    }

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
