/* 哨兵 Sentinel —— 设置子页面：挂载管理（v3.1.0）
 *
 * 只管这一页的字段：**radar**。
 *
 * 顺带放三块「说明书」：
 *
 * 1. **怎么开一个雷达能准确定位到的 Agent 任务** ——
 *    一条可复制的命令，外加「雷达靠哪三样东西定位」的说明；
 * 2. **如何让我的 Agent 被监控**（v3.1.0）——
 *    各 Agent 推荐的启动命令（**带输出重定向**），
 *    专门教那些日志被压缩、接不进来的 Agent 怎么办；
 * 3. **雷达扫描根目录** —— 扫哪儿、在哪儿扫。
 *
 * 另外 v3.3.2 加了一块**快捷重定向命令**：
 * 针对当前这台机器，生成一条把 Agent 输出落到固定文件的命令 ——
 * 解决「终端里跑着任务、看板却抓不到」的老问题。
 */
(function () {
  "use strict";

  var C = window.SentinelSettings;
  var state = { sessions: [] };

  /* -------------------------------------------------------- 会话渲染 */

  function renderSessions() {
    var host = C.byId("session-list");
    if (!host) {
      return;
    }
    host.innerHTML = "";
    if (!state.sessions.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "当前没有正在监控的会话。";
      host.appendChild(empty);
      return;
    }
    state.sessions.forEach(function (session) {
      host.appendChild(buildSessionCard(session));
    });
  }

  function buildSessionCard(session) {
    var card = document.createElement("div");
    card.className = "settings-session";

    var name = document.createElement("span");
    name.className = "settings-session__name";
    name.textContent = String(session.label || session.id || "未命名会话");
    card.appendChild(name);

    var agent = document.createElement("span");
    agent.className = "chip chip--plain mono";
    agent.textContent = String(session.agent || "未知 Agent");
    card.appendChild(agent);

    var stateTag = document.createElement("span");
    stateTag.className = "chip chip--state";
    stateTag.setAttribute("data-state", String(session.state || "idle"));
    stateTag.textContent = String(session.state || "未知");
    card.appendChild(stateTag);

    var meta = document.createElement("div");
    meta.className = "settings-session__meta";

    var workspace = document.createElement("span");
    workspace.className = "settings-session__path";
    /* ⚠️ 提取不到工作目录时**不再写「未知工作区」**（v3.0.1）——
       那句话会让用户以为哨兵坏了。改成说清楚「监控的是哪个文件」。 */
    var workspaceText = session.workspace
      ? session.workspace
      : "未提取到（监控文件 " + C.lastPathPart(session.watch_path) + "）";
    workspace.textContent = "工作区：" + workspaceText;

    var watch = document.createElement("span");
    watch.className = "settings-session__path";
    watch.textContent = "监控日志：" +
      (session.watch_path ? session.watch_path : "未知路径");

    meta.appendChild(workspace);
    meta.appendChild(watch);
    card.appendChild(meta);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn--ghost btn--sm";
    remove.textContent = "卸载";
    remove.addEventListener("click", function () {
      removeSession(String(session.id || ""));
    });
    card.appendChild(remove);

    return card;
  }

  function removeSession(id) {
    if (!id) {
      return;
    }
    setSessionStatus("wait", "正在卸载 " + id + " …");
    fetch("/api/sessions/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          var reason = (data && data.error) || "未知错误";
          setSessionStatus("error", "卸载失败：" + reason);
          return;
        }
        state.sessions = state.sessions.filter(function (item) {
          return String(item.id || "") !== id;
        });
        renderSessions();
        setSessionStatus("ok", "已卸载会话 " + id + "（停止监控）。");
      })
      .catch(function (err) {
        setSessionStatus("error", "卸载失败：" + err.message);
      });
  }

  function setSessionStatus(kind, text) {
    var node = C.byId("session-status");
    if (!node) {
      return;
    }
    node.className = "panel__hint settings-load settings-load--" + kind;
    node.textContent = text || "";
  }

  /* -------------------------------------------------- 一键复制命令 */
  /* 复制「怎么开一个雷达能准确定位到的 Agent 任务」的命令。
   *
   * ⚠️ 命令里的工作目录取**页面所在目录**（服务端渲染时注入的
   *    ``radar_workdir``）—— 那是用户启动哨兵时所在的目录，
   *    一般就是他想干活的那个项目。 */
  function bindCopyButton() {
    var button = C.byId("radar-copy-command");
    if (!button) {
      return;
    }
    button.addEventListener("click", function () {
      var code = C.byId("radar-launch-command");
      var text = code ? code.textContent : "";
      C.copyText(text).then(
        function () {
          button.textContent = "已复制 ✓";
          setSessionStatus("ok", "命令已复制到剪贴板，粘到终端里就能用。");
          window.setTimeout(function () {
            button.textContent = "复制命令";
          }, 2000);
        },
        function (err) {
          /* 复制被拒了（文档没焦点 / 没有用户手势）——
             别只说一句「失败」：把命令**选中**，
             用户按一下 Ctrl+C 就能拿到。 */
          var picked = C.selectElementText(code);
          button.textContent = picked ? "已选中，按 Ctrl+C" : "复制失败";
          setSessionStatus(
            "error",
            picked
              ? "浏览器拒绝了自动复制 —— 命令已选中，按 Ctrl+C 复制即可。"
              : "复制失败：" + err.message
          );
        }
      );
    });
  }

  /* ------------------------------------- v3.1.0「怎么监控我的 Agent」命令 */
  /* 「如何让我的 Agent 被监控」面板里的四个启动命令，各带一个【复制】。
   *
   * 用**事件委托**而不是逐个绑 —— 面板里的条目是静态的，
   * 但以后要加 Agent 的话就不用改 JS 了。 */
  function bindGuideCopyButtons() {
    var root = document.querySelector(".settings-guide");
    if (!root) {
      return;
    }
    root.addEventListener("click", function (event) {
      var button = event.target.closest("[data-copy-target]");
      if (!button) {
        return;
      }
      var code = C.byId(button.getAttribute("data-copy-target"));
      if (!code) {
        return;
      }
      var original = button.textContent;
      C.copyText(code.textContent).then(
        function () {
          button.textContent = "已复制 ✓";
          window.setTimeout(function () {
            button.textContent = original;
          }, 2000);
        },
        function (err) {
          /* 复制被拒 —— 把命令选中，让用户自己按 Ctrl+C。 */
          var picked = C.selectElementText(code);
          button.textContent = picked ? "已选中，按 Ctrl+C" : "复制失败";
          setSessionStatus(
            "error",
            picked
              ? "浏览器拒绝了自动复制 —— 命令已选中，按 Ctrl+C 复制即可。"
              : "复制失败：" + err.message
          );
          window.setTimeout(function () {
            button.textContent = original;
          }, 3000);
        }
      );
    });
  }

  /* ------------------------------------- v3.3.2：快捷重定向命令 */
  /* 「把你自己终端里跑的 Agent 变成雷达能看见的日志」——
   *
   * 一条命令，两件事同时做完：
   *
   * 1. 让 Agent 输出**机器可读**的 JSONL（不然哨兵解析不了）；
   * 2. 用 ``>`` 把输出落到一个**固定的**文件上（雷达扫文件）。
   *
   * ⚠️ 各 Agent 的开关不一样，少一个就白搭：
   *    mcode 缺 ``--output-format stream-json`` 出来的是给人看的文本；
   *    codex 缺 ``--json`` 同理；
   *    claude 缺 ``--print`` 会进交互模式，压根不返回。 */

  var CAPTURE_COMMANDS = {
    mcode: 'mcode exec --output-format stream-json --cwd "{cwd}" "{task}" > "{capture}"',
    codex: 'codex exec --json --cd "{cwd}" "{task}" > "{capture}"',
    claude: 'claude --output-format stream-json --print "{task}" > "{capture}"'
  };

  /**
   * 读出这一页需要的三个路径/输入。
   *
   * @return {Object} ``agent`` / ``task`` / ``workdir`` / ``capture``。
   */
  function captureContext() {
    var root = C.byId("capture-command");
    var agent = C.byId("capture-agent");
    var task = C.byId("capture-task");
    return {
      agent: agent ? String(agent.value || "mcode") : "mcode",
      task: task ? String(task.value || "").trim() : "",
      workdir: root ? String(root.getAttribute("data-workdir") || "") : "",
      capture: root ? String(root.getAttribute("data-capture") || "") : ""
    };
  }

  /**
   * 按当前选择拼出那条重定向命令。
   *
   * @param {Object} ctx captureContext 的返回。
   * @return {string} 可直接粘到终端里的命令。
   */
  function buildCaptureCommand(ctx) {
    var template = CAPTURE_COMMANDS[ctx.agent] || CAPTURE_COMMANDS.mcode;
    return template
      .split("{cwd}").join(ctx.workdir || ".")
      .split("{task}").join(ctx.task || "你的任务指令")
      .split("{capture}").join(ctx.capture || "capture.jsonl");
  }

  /** 把命令与落盘路径刷新到页面上。 */
  function renderCaptureCommand() {
    var ctx = captureContext();
    var code = C.byId("capture-command-text");
    if (code) {
      code.textContent = buildCaptureCommand(ctx);
    }
    var path = C.byId("capture-path-text");
    if (path) {
      path.textContent = ctx.capture || "–";
    }
  }

  /** 绑定「快捷重定向命令」这一块：选 Agent / 改指令 → 重算命令 → 复制。 */
  function bindCaptureCommand() {
    var root = C.byId("capture-command");
    if (!root) {
      return;
    }
    renderCaptureCommand();

    var agent = C.byId("capture-agent");
    if (agent) {
      agent.addEventListener("change", renderCaptureCommand);
    }
    var task = C.byId("capture-task");
    if (task) {
      task.addEventListener("input", renderCaptureCommand);
    }

    var button = C.byId("capture-copy-command");
    if (!button) {
      return;
    }
    button.addEventListener("click", function () {
      var code = C.byId("capture-command-text");
      var text = code ? code.textContent : "";
      C.copyText(text).then(
        function () {
          button.textContent = "已复制 ✓";
          setSessionStatus("ok", "命令已复制到剪贴板，粘到终端里就能用。");
          window.setTimeout(function () {
            button.textContent = "复制命令";
          }, 2000);
        },
        function (err) {
          /* 复制被拒 —— 把命令选中，让用户自己按 Ctrl+C。 */
          var picked = C.selectElementText(code);
          button.textContent = picked ? "已选中，按 Ctrl+C" : "复制失败";
          setSessionStatus(
            "error",
            picked
              ? "浏览器拒绝了自动复制 —— 命令已选中，按 Ctrl+C 复制即可。"
              : "复制失败：" + err.message
          );
        }
      );
    });
  }

  /* -------------------------------------------------------- 表单读取 */

  function collectPayload() {
    return {
      radar: {
        scan_roots: C.splitLines("radar-scan-roots"),
        /* v3.4.0：排除关键词与自定义进程特征。 */
        exclude_keywords: C.splitLines("radar-exclude-keywords"),
        custom_process_features: C.splitLines("radar-custom-features")
      }
    };
  }

  function fillForm(data) {
    state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
    renderSessions();

    /* 雷达扫描根目录（v3.0.1）—— 配置里没写就显示内置默认清单。 */
    var radar = data.radar || {};
    var scanRoots = radar.scan_roots;
    C.setValue(
      "radar-scan-roots",
      Array.isArray(scanRoots) ? scanRoots.join("\n") : ""
    );
    var currentRoots = Array.isArray(data.radar_roots) ? data.radar_roots : [];
    C.setText(
      "radar-roots-current",
      currentRoots.length ? currentRoots.join("　|　") : "–"
    );

    /* v3.4.0：排除关键词与自定义进程特征 ——
     * 显示的是**当前实际生效**的值（后端已经算好了）。 */
    var excludeKeywords = data.radar_exclude_keywords;
    C.setValue(
      "radar-exclude-keywords",
      Array.isArray(excludeKeywords) ? excludeKeywords.join("\n") : ""
    );
    var customFeatures = data.radar_custom_features;
    C.setValue(
      "radar-custom-features",
      Array.isArray(customFeatures) ? customFeatures.join("\n") : ""
    );
  }

  /* ------------------------------------------------------------ 启动 */

  function init() {
    C.bindCommonActions(collectPayload, fillForm);
    bindCopyButton();
    bindGuideCopyButtons();
    bindCaptureCommand();
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
