/* 哨兵 Sentinel —— 设置页公共脚本（v3.0.2）
 *
 * ⚠️ v3.0.2 起设置页是**多页面应用（MPA）**：
 * 每个设置功能一个独立页面，各自有自己的脚本；
 * 这个文件放的是它们**共用**的那部分。
 *
 * 页面契约（与 dashboard.js / router.js 一致）：
 * 每个页面脚本要暴露 ``window.SentinelPage = { init, destroy }``。
 */
(function () {
  "use strict";

  /* 服务端把密钥打码成这个值；原样回传它 = 「我没改这个密钥」 */
  var SECRET_MASK = "********";

  /* ---------------------------------------------------------- 小工具 */

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    var node = byId(id);
    if (node) {
      node.textContent = text === undefined ? "" : String(text);
    }
  }

  function setValue(id, raw) {
    var node = byId(id);
    if (!node) {
      return;
    }
    if (raw === undefined || raw === null) {
      node.value = "";
      return;
    }
    node.value = String(raw);
  }

  function setChecked(id, raw) {
    var node = byId(id);
    if (node) {
      node.checked = !!raw;
    }
  }

  function value(id) {
    var node = byId(id);
    return node ? String(node.value || "") : "";
  }

  function num(id) {
    var parsed = Number(value(id));
    return isFinite(parsed) ? parsed : 0;
  }

  function numOr(raw, fallback) {
    var parsed = Number(raw);
    return isFinite(parsed) ? parsed : fallback;
  }

  function checked(id) {
    var node = byId(id);
    return node ? !!node.checked : false;
  }

  /* 取路径的最后一段（文件名）—— 用来在「提取不到工作区」时
   * 告诉用户「监控的是哪个文件」，而不是干巴巴一句「未知」。 */
  function lastPathPart(path) {
    var text = String(path || "");
    if (!text) {
      return "（路径未知）";
    }
    var parts = text.split(/[\\/]/);
    return parts[parts.length - 1] || text;
  }

  /* 把多行文本框拆成「一行一条」的数组（去掉空行）。 */
  function splitLines(id) {
    return value(id)
      .split(/\r?\n/)
      .map(function (line) {
        return line.trim();
      })
      .filter(function (line) {
        return line.length > 0;
      });
  }

  function setLoadStatus(kind, text) {
    var node = byId("load-status");
    if (!node) {
      return;
    }
    node.className = "settings-load settings-load--" + kind;
    node.textContent = text || "";
  }

  function setSaveStatus(kind, text) {
    var node = byId("save-status");
    if (!node) {
      return;
    }
    node.className = "settings-status settings-status--" + kind;
    node.textContent = text || "";
  }

  function showResult(box, kind, text) {
    if (!box) {
      return;
    }
    box.className = "settings-result settings-result--" + kind;
    box.textContent = text || "";
  }

  /* 逐条列出「没找到（未修改）」的键 —— 服务端给的是 yaml 路径 */
  function renderSkipped(paths) {
    var list = byId("save-skipped");
    if (!list) {
      return;
    }
    list.innerHTML = "";
    (paths || []).forEach(function (path) {
      var item = document.createElement("li");
      item.textContent = String(path) + " 没找到（未修改）";
      list.appendChild(item);
    });
  }

  /* ------------------------------------------------------------ 密钥 */
  /* 规则：输入框里有内容 = 用户改过，用新值；
   *       没内容 = 用户没动，回传原值（打码值原样传回，
   *       服务端会跳过它，绝不会把真密钥覆盖掉）。 */
  function readSecret(inputId, original) {
    var node = byId(inputId);
    if (node && node.value !== "") {
      return node.value;
    }
    return original || "";
  }

  function resetSecret(inputId, original) {
    var node = byId(inputId);
    if (node) {
      node.value = "";
    }
    return markSecretState(node, original);
  }

  /* 密钥框的悬停提示 —— 让用户一眼看出「这个到底配没配」 */
  function markSecretState(input, original) {
    var text = "";
    if (original !== undefined && original !== null) {
      text = String(original);
    }
    if (input) {
      input.title = text === SECRET_MASK
        ? "已配置（服务端已打码；留空 = 不修改）"
        : (text ? "已配置；留空 = 不修改" : "未配置");
    }
    return text;
  }

  /* ------------------------------------------------------- DOM 构件 */

  function makeField(labelText, control) {
    var wrap = document.createElement("div");
    wrap.className = "settings-field";
    var label = document.createElement("label");
    label.className = "settings-label";
    label.setAttribute("for", control.id);
    label.textContent = labelText;
    wrap.appendChild(label);
    wrap.appendChild(control);
    return wrap;
  }

  function makeCheck(box, labelText) {
    var wrap = document.createElement("div");
    wrap.className = "settings-check";
    var label = document.createElement("label");
    label.className = "settings-check__label";
    label.setAttribute("for", box.id);
    label.textContent = labelText;
    wrap.appendChild(box);
    wrap.appendChild(label);
    return wrap;
  }

  function makeInput(id, type, initial, labelText) {
    var input = document.createElement("input");
    input.className = "settings-input";
    input.id = id;
    input.type = type;
    input.value = initial === undefined || initial === null
      ? ""
      : String(initial);
    if (type === "number") {
      input.min = "0";
      input.step = "any";
    }
    return { wrap: makeField(labelText, input), input: input };
  }

  function makeSelect(id, options, current, labelText) {
    var select = document.createElement("select");
    select.className = "settings-select";
    select.id = id;
    options.forEach(function (option) {
      var node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      select.appendChild(node);
    });
    var wanted = current || options[0] || "";
    if (wanted && options.indexOf(wanted) < 0) {
      var extra = document.createElement("option");
      extra.value = wanted;
      extra.textContent = wanted;
      select.appendChild(extra);
    }
    select.value = wanted;
    return { wrap: makeField(labelText, select), input: select };
  }

  /* ------------------------------------------------------------ 复制 */
  /* 复制到剪贴板。
   *
   * ⚠️ ``navigator.clipboard`` 只在**安全上下文**（https 或 localhost）
   *    可用；用 IP + 非标准端口访问时会没有。
   *    所以必须有 ``execCommand`` 这条老路兜底。
   */
  function copyText(text) {
    var value = String(text || "");
    if (!value) {
      return Promise.reject(new Error("没有可复制的内容"));
    }
    /* ⚠️ 先把焦点抢回自己身上
     * ----------------------
     * ``navigator.clipboard.writeText`` 要求**文档处于焦点状态** ——
     * 用户在别的窗口点了一下再回来复制，就会被拒绝。
     * 先 ``focus()`` 一下能救回大多数情况。 */
    try {
      if (typeof window.focus === "function") {
        window.focus();
      }
    } catch (err) {
      /* 拿不到焦点就算了 —— 后面还有老路兜底。 */
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value).catch(function () {
        return legacyCopy(value);
      });
    }
    return legacyCopy(value);
  }

  /* 把某个元素里的文字**选中**（复制失败时的兜底）。
   *
   * ⚠️ 为什么要有这一手
   * ------------------
   * 浏览器在「文档没焦点」「没有用户手势」时会拒绝复制。
   * 这时候与其让用户自己去找那段文字，不如**直接选中**它，
   * 用户按一下 Ctrl+C 就行 —— 总比一句「复制失败」有用。
   *
   * @param {HTMLElement} node 要选中的元素。
   * @return {boolean} 选中了就返回 true。
   */
  function selectElementText(node) {
    if (!node) {
      return false;
    }
    try {
      var range = document.createRange();
      range.selectNodeContents(node);
      var selection = window.getSelection();
      if (!selection) {
        return false;
      }
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    } catch (err) {
      return false;
    }
  }

  function legacyCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(area);
    if (!ok) {
      return Promise.reject(new Error("浏览器拒绝了复制，请手动选中复制"));
    }
    return Promise.resolve();
  }

  /* -------------------------------------------------------- 读取配置 */

  function loadConfig() {
    setLoadStatus("wait", "正在读取配置…");
    return fetch("/api/config")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          var reason = (data && data.error) || "未知错误";
          setLoadStatus("error", "读取配置失败：" + reason);
          return null;
        }
        setLoadStatus("ok", "");
        return data;
      })
      .catch(function (err) {
        setLoadStatus("error", "读取配置失败：" + err.message);
        return null;
      });
  }

  /* -------------------------------------------------------- 保存配置 */

  function saveConfig(payload, afterSave) {
    var button = byId("save-button");
    if (button) {
      button.disabled = true;
      button.textContent = "保存中…";
    }
    setSaveStatus("wait", "正在保存…");
    renderSkipped([]);
    return fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          var reason = (data && data.error) || "未知错误";
          setSaveStatus("error", "保存失败：" + reason);
          renderSkipped(data && data.skipped);
          return null;
        }
        var text = data.message || "已保存。";
        if (data.backup) {
          text += " 配置已备份到 " + data.backup;
        }
        if (data.reloaded) {
          text += " 规则已热重载。";
        } else if (data.reload_note) {
          text += " " + data.reload_note;
        }
        setSaveStatus("ok", text);
        renderSkipped(data.skipped);
        /* 存完再读一遍 —— 让页面显示的永远是磁盘上的真实状态 */
        return loadConfig().then(function (fresh) {
          if (fresh && typeof afterSave === "function") {
            afterSave(fresh);
          }
          return fresh;
        });
      })
      .catch(function (err) {
        setSaveStatus("error", "保存失败：" + err.message);
        return null;
      })
      .then(function (result) {
        if (button) {
          button.disabled = false;
          button.textContent = "保存并应用";
        }
        return result;
      });
  }

  /* ------------------------------------------------------- 通用按钮 */
  /* 把【保存并应用】/【重新载入】接到当前页面的
   * 「收集表单」与「回填表单」上。 */
  function bindCommonActions(collect, fill) {
    var saveButton = byId("save-button");
    if (saveButton) {
      saveButton.addEventListener("click", function () {
        saveConfig(collect(), fill);
      });
    }
    var reloadButton = byId("reload-button");
    if (reloadButton) {
      reloadButton.addEventListener("click", function () {
        loadConfig().then(function (data) {
          if (data && typeof fill === "function") {
            fill(data);
          }
        });
      });
    }
  }

  /* ------------------------------------------------------------ 启动 */
  /* 页面生命周期钩子 —— 前端路由（router.js）的契约：
   * 换页后路由器会统一调 init()；直接打开本页时则自己调。
   * ⚠️ 两个入口只能走一个，否则事件会绑两遍、配置会读两遍。 */
  function boot(init) {
    if (window.__SENTINEL_ROUTING__) {
      /* 路由器正在换页，马上会替我们调 init() —— 这里别再初始化一遍。 */
      return;
    }
    init();
  }

  window.SentinelSettings = {
    SECRET_MASK: SECRET_MASK,
    bindCommonActions: bindCommonActions,
    boot: boot,
    byId: byId,
    checked: checked,
    copyText: copyText,
    selectElementText: selectElementText,
    lastPathPart: lastPathPart,
    loadConfig: loadConfig,
    makeCheck: makeCheck,
    makeField: makeField,
    makeInput: makeInput,
    makeSelect: makeSelect,
    markSecretState: markSecretState,
    num: num,
    numOr: numOr,
    readSecret: readSecret,
    renderSkipped: renderSkipped,
    resetSecret: resetSecret,
    saveConfig: saveConfig,
    setChecked: setChecked,
    setLoadStatus: setLoadStatus,
    setSaveStatus: setSaveStatus,
    setText: setText,
    setValue: setValue,
    showResult: showResult,
    splitLines: splitLines,
    value: value
  };
})();
