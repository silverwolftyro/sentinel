/* 哨兵 Sentinel —— 监控面板前端
 *
 * 纯浏览器原生 API：EventSource 收 SSE，fetch 拉状态与触发扫描。
 * 没有构建步骤、没有依赖，静态文件直接由 FastAPI 伺服。
 */
(function () {
  "use strict";

  var MAX_LOG_LINES = 300;
  var MAX_CONSOLE_LINES = 200;

  /* 「建议武装」的置信度门槛（v3.0.0）——
   *
   * 进程反查的置信度 >= 0.95 时，哨兵**基本确定**自己认对了人
   * （命令行里带着这个日志路径、或者进程正打开着这个文件）。
   * 这时候把【武装守护者】开关**高亮**出来，
   * 并用气泡提示一句「高置信度匹配，建议开启守护者」——
   *
   * ⚠️ 只是**建议**，绝不自动武装：
   * 挂起别人的进程这件事，必须用户自己点头（红线 R7）。 */
  var ARM_SUGGEST_CONFIDENCE = 0.95;

  /* Agent 类型的显示名（v2.3.0）——
   * 后端给的是小写标识（codex / mcode / claude），
   * 界面上要显示成产品名（Codex / MCode / Claude）。 */
  var AGENT_LABELS = {
    codex: "Codex",
    mcode: "MCode",
    claude: "Claude"
  };

  /* 「没设目标」的强引导文案（v3.3.1）——
   *
   * 挂载进来的会话常常没有初始 Prompt —— 雷达找不到初始目标。
   * 这时候顶部那一句「未设置」**必须**说清楚下一步该干什么，
   * 否则用户只能对着空面板发呆。 */
  var GOAL_NUDGE_TEXT =
    "雷达找不到初始目标，请在此手动输入任务目标，以启用漂移监控。";

  /* 「没绑定到进程」的说明（v3.3.1）——
   *
   * 查不到 PID 是一件**正常**的事：用户重定向了输出，
   * 或者用了特殊启动方式 —— 命令行里本来就没有日志路径。
   *
   * ⚠️ 它**只影响「拦截」这一项能力**：
   * L1/L2 判定、告警落盘、通知推送全都照常工作 ——
   * 哨兵本来就是**只读**监控（红线 R1 / R2）。
   * 所以这句话必须说清楚：不是「坏了」，是「少了一项能力」。 */
  var UNBOUND_PID_HINT =
    "未找到对应进程。如需拦截，请使用 --agent 模式启动，" +
    "或确保 Agent 命令行包含日志路径。";

  /* 没绑定到进程时，追加在提示后面的一句 ——
   * 让用户知道「告警照样会来」，不必因为灰掉的开关就放弃使用。 */
  var UNBOUND_PID_STILL_WORKS =
    "L1/L2 告警与推送不受影响 —— 只是无法挂起进程。";

  /* 「日志跟随锁定」的底部判定阈值（v3.5.1）——
   *
   * 离底部不到 50 像素就算「用户还在底部」。
   * 留一点余量是必要的 —— 浏览器的滚动残差
   * 会让「差一点点到顶」也触发 scroll 事件，
   * 卡得太死会把跟随**误判**成解除，用户莫名其妙就不跟了。 */
  var LOG_FOLLOW_THRESHOLD = 50;

  /* 流水区的三个颜色等级（v3.5.1）——
   *
   * 绿 = 正常运行；橘黄 = 可能有问题；红 = 严重问题。
   * 写成常量而不是散落的字符串 —— 「用户到底会看到哪种颜色」
   * 必须有一处可以钉住的地方。 */
  var AUDIT_LEVEL_OK = "ok";
  var AUDIT_LEVEL_WARN = "warn";
  var AUDIT_LEVEL_DANGER = "danger";

  /* 流水区引用的原文最多保留多少字（v3.5.1）——
   * 与后端 :data:`src.core.live_audit.MAX_SNIPPET_LENGTH` 同一个口径。 */
  var AUDIT_SNIPPET_LIMIT = 40;

  /* 「没发现活跃 Agent」的引导文案（v3.5.1）——
   *
   * 雷达空荡荡的时候，光写「未发现」等于把用户晾在原地 ——
   * 他不知道该去哪儿、也不知道该敲什么。
   * 所以必须**直接把可执行的那一条命令**摆出来。 */
  var RADAR_EMPTY_TITLE =
    "未发现正在运行的 Agent。若要开始监控，请在终端中运行：";

  /* 空态引导里用的**相对**路径（v3.6.0）——
   *
   * ⚠️ v3.5.1 的空态把**绝对**路径摆出来了，可那个文件当时
   *    **根本不存在** —— 用户照抄命令、点【接入】，
   *    换回来的却是一句冷冰冰的 400。
   *    现在文件没生成时，只给**相对**路径（相对他自己终端的
   *    当前目录），并明说「先生成再接入」。
   *    绝不假装文件已经就绪。 */
  var RADAR_EMPTY_RELATIVE_PATH = "logs/capture.jsonl";

  /* 空态引导的两种收尾语（v3.6.0）——
   * 文件已经在 → 可以直接接入；还没生成 → 先让他跑起来。 */
  var RADAR_EMPTY_NOTE_READY =
    "跑起来后点上方【＋ 发现/挂载进程】接入。";
  var RADAR_EMPTY_NOTE_MISSING =
    "请先运行该命令，生成文件后再点击接入。";

  /* --- v3.7.0：呈现档位（服务端配置，主看板不再有滑条）---
   *
   * ⚠️ v3.6.0 的日志档位滑条在 v3.7.0 **整条移除** ——
   *    档位改由设置页「呈现与摘要」配（``presentation.log_compression``），
   *    看板启动时从 ``/api/presentation`` 读，**所有窗口共享同一份**。
   *
   * 档位是**连续**的（0-100），行为分四段：
   *   0  - 30  纯代码：原始 JSON（带高亮）
   *   31 - 69  自然语言：🧑 用户提问 / 🤖 Agent 回答 / 💭 思考
   *   70 - 99  高度摘要：只留对话主干，丢掉思考过程
   *   100      极限摘要：连续同类动作**真的合并**成一行
   *             （连续 5 次读文件 → ``[读取] core/auth.py 等 5 个文件``）
   */
  var LOG_SCALE_MIN = 0;
  var LOG_SCALE_MAX = 100;
  var LOG_SCALE_DEFAULT = 40;
  var LOG_SCALE_CODE_MAX = 30;
  var LOG_SCALE_SUMMARY_MIN = 70;
  var LOG_SCALE_EXTREME = 100;

  /* --- v3.7.0：审核流水的压缩档位 ---
   *
   * ⚠️ 边界必须与设置页 ``settings_pipeline.js`` 的 AUDIT_SCALE_BANDS 一致：
   *   0  - 39  完整：所有条目（含「Agent 正在思考」）
   *   40 - 69  精简：省略思考过程
   *   70 - 89  结论：只留核心结论与告警
   *   90 - 100 极限：在「结论」之上，连续的普通流水合并成一条计数行
   */
  var AUDIT_SCALE_DEFAULT = 60;
  var AUDIT_SCALE_THINKING_MAX = 39;
  var AUDIT_SCALE_CONCLUSION_MIN = 70;
  var AUDIT_SCALE_EXTREME_MIN = 90;

  /* --- v3.7.0：动作分类（极限摘要的合并依据）---
   *
   * 摘要里只保留**动作类型**（读 / 写 / 跑）与**核心目标**——
   * 参数、结果一律不进摘要。
   */
  var LOG_ACTION_READ = "read";
  var LOG_ACTION_WRITE = "write";
  var LOG_ACTION_EXEC = "exec";
  var LOG_ACTION_OTHER = "other";

  var LOG_ACTION_LABELS = {
    read: "读取",
    write: "写入",
    exec: "执行",
    other: "动作"
  };

  /* 摘要里目标最多留多少字（v3.7.0）——
   * 路径从**前面**截（尾巴才是信息量所在），
   * 命令从**后面**截（开头才是动作）。 */
  var LOG_TARGET_LIMIT = 64;

  /* 面板布局的持久化键（v3.6.0）——
   * 拖出来的比例存这里，刷新页面还是上次的手感。 */
  var LAYOUT_STORAGE_KEY = "sentinel.layout.v1";

  /* 拖拽的边界值（v3.6.0）——
   * ⚠️ 上下限是硬约束 —— 拖到 0 会让面板彻底消失，
   *    那样用户连「拖回去」的分隔条都找不到了。 */
  var LAYOUT_MIN_SESSIONS = 180;
  var LAYOUT_MAX_SESSIONS = 520;
  var LAYOUT_MIN_STREAM = 260;
  var LAYOUT_MIN_ALERTS = 280;
  var LAYOUT_MIN_AUDIT = 80;
  var LAYOUT_MIN_ALERT_AREA = 120;

  /* 自然语言模式下正文最多保留多少字（v3.6.0）——
   * 超出就截断加省略号 —— 一行太长反而没法读。 */
  var LOG_NATURAL_LIMIT = 400;
  var LOG_SUMMARY_LIMIT = 220;

  /* 极限摘要（100 档）只保留**前两句**（v3.9.0）——
   * 一句快讯说清「刚才发生了什么」就够了，
   * 再多就变成「代码审查报告」了。 */
  var LOG_EXTREME_SENTENCES = 2;

  /**
   * Agent 类型的显示名。
   *
   * @param {string} name 小写标识。
   * @return {string} 显示名；认不出来时原样返回。
   */
  function agentLabel(name) {
    var key = String(name || "").toLowerCase();
    return AGENT_LABELS[key] || String(name || "");
  }

  var el = {
    alertList: document.getElementById("alert-list"),
    alertEmpty: document.getElementById("alert-empty"),
    /* --- v3.3.1：实时审核流水 --- */
    liveAuditLines: document.getElementById("live-audit-lines"),
    liveAuditHint: document.getElementById("live-audit-hint"),
    logLines: document.getElementById("log-lines"),
    logEmpty: document.getElementById("log-empty"),
    runState: document.getElementById("run-state"),
    countDangerous: document.getElementById("count-dangerous"),
    countLoop: document.getElementById("count-loop"),
    countOnTrack: document.getElementById("count-ontrack"),
    countTotal: document.getElementById("count-total"),
    countToken: document.getElementById("count-token"),
    countTokenBox: document.getElementById("count-token-box"),
    /* --- v3.5.0：级联审核计数器 --- */
    countCascadeBox: document.getElementById("count-cascade-box"),
    countCascadeText: document.getElementById("count-cascade-text"),
    /* --- v3.5.0：虚拟会话筛选 --- */
    streamFilter: document.getElementById("stream-filter"),
    streamFilterText: document.getElementById("stream-filter-text"),
    scanButton: document.getElementById("scan-button"),
    pauseButton: document.getElementById("pause-button"),
    briefingButton: document.getElementById("briefing-button"),
    timelineButton: document.getElementById("timeline-button"),
    healthButton: document.getElementById("health-button"),
    attachButton: document.getElementById("attach-button"),
    /* --- v3.3.2：雷达的「活跃进程感知」 --- */
    radarAlert: document.getElementById("radar-alert"),
    radarAlertTitle: document.getElementById("radar-alert-title"),
    /* --- v3.5.1：雷达空态引导 --- */
    radarEmpty: document.getElementById("radar-empty"),
    /* --- v3.5.1：日志跟随锁定 --- */
    logFollowButton: document.getElementById("log-follow-button"),
    /* --- v3.6.0：多面板拖拽与折叠 --- */
    bodyLayout: document.getElementById("body-layout"),
    resizerSessions: document.getElementById("resizer-sessions"),
    resizerStream: document.getElementById("resizer-stream"),
    resizerAudit: document.getElementById("resizer-audit"),
    alertPanel: document.getElementById("alert-panel"),
    streamPanel: document.getElementById("stream-panel"),
    liveAuditBox: document.getElementById("live-audit"),
    sessionsCollapse: document.getElementById("sessions-collapse"),
    sessionsRail: document.getElementById("sessions-rail"),
    sessionsExpand: document.getElementById("sessions-expand"),
    sessionsRailCount: document.getElementById("sessions-rail-count"),
    /* --- v3.5.1：Status Pulse 呼吸灯 --- */
    statusPulse: document.getElementById("status-pulse"),
    /* --- v3.4.0：彻底关闭哨兵 --- */
    shutdownButton: document.getElementById("shutdown-button"),
    settingsLink: document.getElementById("settings-link"),
    consoleToggle: document.getElementById("console-toggle"),
    consoleBody: document.getElementById("console-body"),
    consoleLines: document.getElementById("console-lines"),
    consoleCount: document.getElementById("console-count"),
    guardianChip: document.getElementById("guardian-chip"),
    goalForm: document.getElementById("goal-form"),
    goalInput: document.getElementById("goal-input"),
    goalHint: document.getElementById("goal-hint"),
    watchTarget: document.getElementById("watch-target"),
    watchAgent: document.getElementById("watch-agent"),
    watchGoal: document.getElementById("watch-goal"),
    watchGoalSource: document.getElementById("watch-goal-source"),
    watchGoalNudge: document.getElementById("watch-goal-nudge"),
    sessionPanel: document.getElementById("session-panel"),
    sessionList: document.getElementById("session-list"),
    sessionsHint: document.getElementById("sessions-hint"),
    globalStateChips: document.getElementById("global-state-chips"),
    toast: document.getElementById("toast"),
    /* --- v3.9.0：移动端沉浸式 Tab 栏 --- */
    mobileTabs: document.getElementById("mobile-tabs"),
    /* --- v3.0.1：退出演示 --- */
    demoExitButton: document.getElementById("demo-exit-button"),
    demoExitPanel: document.getElementById("demo-exit-panel"),
    demoExitCode: document.getElementById("demo-exit-code")
  };

  var state = {
    counts: {},
    scanCounts: {},
    alerts: [],
    paused: false,
    taskGoal: "",
    watchTarget: { agent: "", goal: "", goal_source: "none" },
    guardian: { available: false, enabled: false },
    /* --- Token 审计（v1.1.0）--- */
    tokens: { total_tokens: 0, estimated: false },
    /* --- 多路会话（v1.0.0）--- */
    sessions: [],
    activeSession: "",
    /* --- 已渲染的告警集合（v1.2.0）---
     * 列表没变就不重建卡片 —— 见 renderAlerts 的说明。 */
    renderedAlertIds: "",
    /* --- 已渲染的会话列表签名（v4.1.0）---
     * 签名没变就不重建条目 —— 见 renderSessionList 的说明。 */
    renderedSessionSig: "",
    /* --- 已渲染的实时流水签名（v4.1.0）---
     * 签名没变就不重建流水区 —— 见 renderLiveAudit 的说明。 */
    renderedLiveAuditSig: "",
    /* --- 实时审核流水（v3.3.1）---
     * ON_TRACK 判定的极简摘要；只是**渲染**用的缓存。 */
    liveAudit: [],
    /* --- v3.5.0：未筛选的流水列表 ---
     * 切换虚拟会话时要拿它**重画**，所以不能只留筛选后的。 */
    liveAuditAll: [],
    /* --- v3.7.0：流水极限摘要的「连续普通流水」计数行 ---
     *
     * 100 档时连续的普通流水会被合并成一行计数。
     * 这里记住**当前正在累积的那一串** ——
     * 下一条不是普通流水时，这一串就封口了。
     * ⚠️ 只是**渲染**用的临时状态，不是业务数据。 */
    auditPlainRun: null,
    /* --- 活跃进程感知（v3.3.2）---
     * 「快捷重定向命令」默认落到的文件路径；点提示框时预填到输入框。 */
    radarCapturePath: "",
    /* --- v3.5.0：虚拟会话（线程指纹）---
     *
     * 一份日志里混着多个 Agent 时，后端按**指纹**把它们分成多路；
     * 前端据此把左侧拆成多张**虚拟会话卡片**。
     * ``streamFilter.sessionId`` + ``streamFilter.key`` 记着
     * 「用户现在只想看哪一路」。 */
    streamFilter: { sessionId: "", key: "" },
    /* --- v3.5.0：级联审核统计 --- */
    cascade: {},
    /* --- v3.5.1：日志跟随锁定 ---
     * true = 跟随中（用户停在底部，新日志来了自动滚到底）；
     * false = 已解除（用户往上翻历史，哨兵**绝不**再把他拽回底部）。 */
    logFollow: true,
    /* --- v3.7.0：呈现档位（服务端配置）---
     * ⚠️ v3.6.0 这里是 ``logScale``（本地滑条）——
     *    v3.7.0 起档位来自 ``/api/presentation``，看板**不再有滑条**。 */
    logCompression: LOG_SCALE_DEFAULT,
    auditCompression: AUDIT_SCALE_DEFAULT,
    /* --- v3.6.0：原始日志行缓存 ---
     *
     * 档位一变就要**重画**整块日志 ——
     * 但重画不该重新拉后端（那会闪），
     * 所以这里留着**原始行**，随时能自己画回来。
     * ⚠️ 比对「内容变没变」也必须用它 ——
     *    自然语言模式下 DOM 上的文字根本不是原始行。 */
    logRaw: [],
    logRawBase: 0,
    /* --- v3.6.0：雷达捕获文件是否已经存在 --- */
    radarCaptureExists: false,
    /* --- v3.6.0：面板尺寸（拖拽结果）--- */
    layout: { sessions: 0, stream: 0, audit: 0, collapsed: false }
  };
  var consoleSeq = 0;
  var toastTimer = null;
  /* 日志面板的滚动容器（v3.5.1）——
   * 记下来是为了 destroy() 时能把监听摘掉。 */
  var logScrollNode = null;
  /* --- 路由生命周期（v2.3.0）---
   *
   * 前端路由会**整页替换** body —— 离开看板以后，
   * EventSource 与定时器必须**收掉**，
   * 不然后台还在轮询、还在收 SSE，又费电又串页。
   */
  var eventSource = null;
  var timers = [];

  /* ----------------------------------------------------- 日志跟随锁定（v3.5.1） */
  //
  // v3.5.0 之前，右侧「被监控日志」是**无条件**滚到底的：
  // 新日志一来就把用户拽回最底下。
  // 用户想回看刚才那段历史根本不可能 —— 滚上去，两秒后又被拉回来。
  //
  // v3.5.1 改成**跟随锁定**：
  //   * 用户在底部 → 跟随中，新日志来了自动滚到底；
  //   * 用户往上滚 → **立刻**解除跟随，哨兵不再打扰；
  //   * 用户滚回底部 → 跟随自动恢复；
  //   * 面板右下角常驻【↓ 回到最新】，不在底部时高亮，
  //     点一下平滑滚到底并恢复跟随。

  /**
   * 取日志面板的滚动容器。
   *
   * 滚动的是 ``#log-lines`` 的**父容器**（``.panel__body``）——
   * 日志行本身不该滚动，滚动的是整块面板。
   *
   * @return {HTMLElement|null} 滚动容器；还没渲染时返回 null。
   */
  function logScrollBox() {
    if (!el.logLines) {
      return null;
    }
    return el.logLines.parentElement;
  }

  /**
   * 用户现在是不是还停在日志底部。
   *
   * @return {boolean} 在底部返回 ``true``。
   */
  function logAtBottom() {
    var box = logScrollBox();
    if (!box) {
      return true;
    }
    var gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    return gap < LOG_FOLLOW_THRESHOLD;
  }

  /**
   * 刷新【回到最新】按钮的显隐与高亮。
   *
   * 只有**脱离跟随**的时候才亮出来 ——
   * 跟着的时候它没有用，亮着反而是噪音。
   */
  function updateLogFollowUi() {
    var button = el.logFollowButton;
    if (!button) {
      return;
    }
    if (state.logFollow) {
      button.hidden = true;
      button.classList.remove("log-follow--visible");
      return;
    }
    button.hidden = false;
    button.classList.add("log-follow--visible");
  }

  /**
   * 平滑滚到日志底部，并**恢复跟随**。
   */
  function scrollLogToLatest() {
    var box = logScrollBox();
    if (!box) {
      return;
    }
    try {
      box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    } catch (err) {
      // 老浏览器没有 scrollTo(options) —— 退回直接定位。
      console.error("平滑滚动失败，改用直接定位：", err);
      box.scrollTop = box.scrollHeight;
    }
    state.logFollow = true;
    updateLogFollowUi();
  }

  /**
   * 日志面板的滚动事件（v3.5.1）。
   *
   * 用户往上翻 → **立刻**解除跟随；
   * 用户自己滚回底部 → 重新跟随。
   */
  function onLogScroll() {
    state.logFollow = logAtBottom();
    updateLogFollowUi();
  }

  /**
   * 判断这次轮询回来的日志是不是**和屏幕上一样**（v3.5.1）。
   *
   * ⚠️ 为什么要加这一层：
   *   v3.5.0 之前每 15 秒就把整块日志**清空重画** ——
   *   用户刚往上翻了两屏历史，下一次轮询就把位置冲没了，
   *   「跟随锁定」根本锁不住。
   *   现在**内容没变就不重画**，用户的阅读位置才留得住。
   *
   * @param {Array<string>} lines 服务端回传的最近日志行。
   * @return {boolean} 没变化返回 ``true``。
   */
  function logContentUnchanged(lines) {
    if (!el.logLines || !lines || !lines.length) {
      return false;
    }
    // ⚠️ v3.6.0：必须拿**原始行**比，不能拿 DOM 上的文字比 ——
    //   自然语言模式下 DOM 里写的是「🧑 用户提问：xxx」，
    //   跟原始 JSON 根本不是一回事，拿它比等于**每 15 秒重画一次**，
    //   用户的阅读位置会被反复冲掉（v3.5.1 修过的坑，不能重犯）。
    if (!state.logRaw.length) {
      return false;
    }
    var last = state.logRaw[state.logRaw.length - 1];
    return last === String(lines[lines.length - 1] || "");
  }

  /**
   * 用一整批日志行**重画**日志面板（v3.5.1）。
   *
   * 脱离跟随时会把滚动位置**放回原处** ——
   * 内容是同一批日志，位置当然还是那一屏。
   *
   * @param {Array<string>} lines 日志行文本数组。
   */
  function replaceLogLines(lines) {
    var box = logScrollBox();
    var keep = box ? box.scrollTop : 0;
    var follow = state.logFollow;
    // v3.6.0：原始行先进缓存，重画走缓存 —— 档位随时能切回来。
    state.logRaw = (lines || []).map(function (item) {
      return String(item == null ? "" : item);
    });
    state.logRawBase = 0;
    redrawLogLines();
    if (!follow && box) {
      box.scrollTop = keep;
    }
  }

  /* ------------------------------------------ 日志渲染档位（v3.6.0） */
  //
  // v3.5.2 的「被监控日志」是**一坨原始 JSON**：
  // 用户反馈「原始 JSON 乱码无法阅读」——
  // 一行几百个字符，引号里套着转义，根本没法看。
  //
  // v3.6.0 加一条**无极滑条**：
  //   0 - 30   纯代码：原始 JSON（带高亮）—— 给要抠细节的人
  //   30 - 70  自然语言：🧑 用户提问 / 🤖 Agent 回答 / 💭 思考
  //   70 - 100 高度摘要：只留对话主干，思考过程全部丢掉
  //
  // ⚠️ 调节时**平滑重绘**：原始行一直在内存里（state.logRaw），
  //    重画只是换 DOM，不重新拉后端 —— 所以不闪。

  /* 解析 JSON 时最多往下钻几层（v3.6.0）——
   * 够覆盖「日志 → message → content[] → text」这种常见嵌套，
   * 又不至于在一份巨大的 JSON 里无限递归。 */
  var LOG_PARSE_MAX_DEPTH = 6;

  /* 噪声段（v3.6.0）——
   * 这些是 Agent 框架塞给模型看的**内部提示**，
   * 不是用户说的话，也不该被当成重点高亮出来。 */
  var LOG_NOISE_TAGS = [
    "system-reminder",
    "background-task-finished",
    "agent-context",
    "environment_context",
    "skills_instructions",
    "cwd"
  ];

  /**
   * 把 JSON 里的噪声段**挖掉**（v3.6.0）。
   *
   * ⚠️ 为什么是「挖掉」而不是「整行丢掉」：
   *   codex 的用户消息里，真实问题跟 ``<environment_context>``
   *   挤在**同一条**消息里 —— 整行丢掉，用户的问题也就没了。
   *   所以只把噪声**段落**去掉，正文留着。
   *
   * @param {string} text 原始正文。
   * @return {string} 去掉噪声段落之后的正文。
   */
  function stripNoiseSections(text) {
    var value = String(text == null ? "" : text);
    if (!value) {
      return "";
    }
    for (var i = 0; i < LOG_NOISE_TAGS.length; i += 1) {
      var open = "<" + LOG_NOISE_TAGS[i] + ">";
      var close = "</" + LOG_NOISE_TAGS[i] + ">";
      var start = value.indexOf(open);
      while (start !== -1) {
        var end = value.indexOf(close, start + open.length);
        if (end === -1) {
          // 没有闭合标签 —— 只去掉开标签本身。
          value = value.slice(0, start) + value.slice(start + open.length);
        } else {
          value = value.slice(0, start) + value.slice(end + close.length);
        }
        start = value.indexOf(open);
      }
    }
    // 独立成行的噪声标记（没有成对的标签）。
    value = value.replace(/^[ \t]*session_identity_reminder.*$/gm, "");
    return value.trim();
  }

  /* 极限摘要的两个原语（v3.9.0）——
   *
   * ⚠️ 这两个函数与后端 ``src/core/live_audit.py`` 里的
   *    ``strip_code_blocks`` / ``first_sentences`` **必须同算法** ——
   *    两边挖的是同一批代码块、同一批句子边界，
   *    不然就会出现「流水区干净、日志面板里全是代码」的分裂。
   *    一致性由 ``tests/test_v390_release.py`` 逐条断言。
   */

  /**
   * 剥离正文里所有被 ``` 包裹的代码块（v3.9.0）。
   *
   * 规则：
   *   * 只有**行首**（允许前导空白）出现 ``` 才算围栏；
   *   * 围栏行整行丢掉，围栏之间的内容整段丢掉；
   *   * **未闭合**的围栏会把后面全部丢掉（宁可少一句，
   *     也不能把代码漏出去）。
   *
   * @param {string} text 原始正文。
   * @return {string} 去掉代码块之后的正文。
   */
  function stripLogCodeBlocks(text) {
    var value = String(text == null ? "" : text);
    if (value.indexOf("```") === -1) {
      return value;
    }
    var lines = value.split("\n");
    var kept = [];
    var inBlock = false;
    for (var i = 0; i < lines.length; i += 1) {
      if (lines[i].trim().indexOf("```") === 0) {
        inBlock = !inBlock;
        continue;
      }
      if (!inBlock) {
        kept.push(lines[i]);
      }
    }
    return kept.join("\n").trim();
  }

  /**
   * 只取正文的前 N 句话（v3.9.0）。
   *
   * 「一句」= 中文 ``。！？；`` 或英文 ``.`` / ``!`` / ``?``；
   * 英文标点**必须**后面是空白或文本结尾才算 ——
   * 否则 ``engine.py`` / ``v1.5`` 会被从中间劈开。
   *
   * @param {string} text 原始正文。
   * @param {number} count 最多取几句。
   * @return {string} 前几句组成的文本；不足时返回全文。
   */
  function firstSentences(text, count) {
    var value = String(text == null ? "" : text).trim();
    var limit = Number(count);
    if (!isFinite(limit) || limit <= 0) {
      limit = 2;
    }
    if (!value) {
      return "";
    }
    var length = value.length;
    var index = 0;
    var end = 0;
    var found = 0;
    while (index < length && found < limit) {
      var char = value.charAt(index);
      if ("。！？!?；;".indexOf(char) !== -1) {
        index += 1;
        end = index;
        found += 1;
        continue;
      }
      if (char === "." || char === "!") {
        var next = index + 1 < length ? value.charAt(index + 1) : "";
        if (next === "" || /^\s$/.test(next)) {
          index += 1;
          end = index;
          found += 1;
          continue;
        }
      }
      index += 1;
    }
    if (found < limit) {
      return value;
    }
    return value.slice(0, end).trim();
  }

  /**
   * 从任意值里收集文本（v3.6.0）。
   *
   * 日志里的正文可能藏在三种地方：
   * 直接的字符串、``[{"type":"text","text":"…"}]`` 这样的数组、
   * 或者再包一层 ``content``。这里统统摊平成一段文字。
   *
   * @param {*} value 待收集的值。
   * @param {number} depth 当前深度。
   * @return {string} 收集到的文本。
   */
  function collectLogText(value, depth) {
    if (depth > LOG_PARSE_MAX_DEPTH || value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    if (Array.isArray(value)) {
      var parts = [];
      for (var i = 0; i < value.length; i += 1) {
        var piece = collectLogText(value[i], depth + 1);
        if (piece) {
          parts.push(piece);
        }
      }
      return parts.join("\n");
    }
    if (typeof value === "object") {
      if (typeof value.text === "string") {
        return value.text;
      }
      if (typeof value.summary === "string") {
        return value.summary;
      }
      if (typeof value.content === "string") {
        return value.content;
      }
      /* ⚠️ ``thinking`` / ``reasoning`` 也要认 ——
       *    后端适配器 :data:`src.adapters.base.BaseAdapter._parse_block`
       *    就是用这两个键取思考正文的，前端不认就等于
       *    「后端看得到思考，看板只显示『其他』」。 */
      if (typeof value.thinking === "string") {
        return value.thinking;
      }
      if (typeof value.reasoning === "string") {
        return value.reasoning;
      }
      if (value.content !== undefined) {
        return collectLogText(value.content, depth + 1);
      }
    }
    return "";
  }

  /**
   * 判断一个节点属于哪一类事件（v3.6.0）。
   *
   * 认得这些形态（真实日志里都出现过）：
   *
   * * ``{"type":"user_message"}`` / ``{"type":"user"}`` /
   *   ``{"type":"prompt"}`` / ``{"type":"human"}`` → 用户
   * * ``{"role":"user"}`` → 用户
   * * ``{"type":"text"}`` / ``{"role":"assistant"}`` → Agent 回答
   * * ``{"type":"thinking"}`` → 思考
   * * ``{"type":"tool_call"}`` → 工具调用
   * * ``{"type":"tool_result"}`` → 工具结果
   * * ``{"role":"compactionSummary"}`` → 上下文摘要
   *
   * @param {object} node 待判断的节点。
   * @return {string} 类别；认不出来返回空串。
   */
  function classifyLogNode(node) {
    if (!node || typeof node !== "object") {
      return "";
    }
    var type = String(node.type || "").toLowerCase();
    var role = String(node.role || "").toLowerCase();
    if (
      type === "user_message" ||
      type === "user" ||
      type === "prompt" ||
      type === "human"
    ) {
      return "user";
    }
    if (type === "thinking" || type === "reasoning") {
      return "think";
    }
    /* ⚠️ ``tool-call``（带连字符）也要认 ——
     *    后端适配器 :data:`src.adapters.base.BaseAdapter._parse_block`
     *    认这个形态，前端不认就等于「后端解析出来了、看板解析不出来」。 */
    if (
      type === "tool_call" ||
      type === "tool_use" ||
      type === "tool-call" ||
      type === "function_call"
    ) {
      return "tool";
    }
    if (type === "tool_result" || type === "tool_response") {
      return "tool_result";
    }
    if (type === "result" || type === "turn_complete") {
      return "result";
    }
    if (
      type === "text" ||
      type === "agent_message" ||
      type === "assistant_message"
    ) {
      return "reply";
    }
    if (role === "user" || role === "human") {
      return "user";
    }
    if (role === "assistant") {
      return "reply";
    }
    if (role === "compactionsummary") {
      return "context";
    }
    return "";
  }

  /**
   * 在 JSON 树里找**工具调用**节点（v3.8.0）。
   *
   * ⚠️ 为什么不能只靠「深度优先、前序、首个命中即返回」——
   *    Claude 风格的**同一行**事件里，
   *    ``thinking`` 块常常排在 ``tool_use`` 块**前面**，
   *    而外层 ``message`` 节点自己就带着 ``role:"assistant"``。
   *    前序遍历会先命中思考块或外层回答块，
   *    **工具调用直接被顶掉**（v3.7.0 遗留的盲区）。
   *
   * 所以这里单独走一趟「找工具」的遍历：
   * **整棵树里只要有一个工具调用节点，就以它为准。**
   *
   * ⚠️ 已知取舍（如实写明，不藏着）：
   *    * 形如 ``{"role":"user","content":[{"type":"tool_use"}]}``
   *      的行会从「用户」改判为「工具」。真实协议里 ``tool_use``
   *      只出现在 assistant 消息中，所以这个风险很低；
   *    * 一行里有**多个**工具块（Claude 并行工具调用）时，
   *      只认第一个 —— 一行只出一条目，这是看板的既有模型。
   *
   * @param {object} node 当前节点。
   * @param {number} depth 当前深度。
   * @return {object|null} 命中的工具节点；没有返回 null。
   */
  function findToolSignal(node, depth) {
    if (!node || typeof node !== "object" || depth > LOG_PARSE_MAX_DEPTH) {
      return null;
    }
    if (classifyLogNode(node) === "tool") {
      return node;
    }
    var keys = Object.keys(node);
    for (var i = 0; i < keys.length; i += 1) {
      var child = node[keys[i]];
      if (!child || typeof child !== "object") {
        continue;
      }
      if (Array.isArray(child)) {
        for (var j = 0; j < child.length; j += 1) {
          var hit = findToolSignal(child[j], depth + 1);
          if (hit) {
            return hit;
          }
        }
        continue;
      }
      var found = findToolSignal(child, depth + 1);
      if (found) {
        return found;
      }
    }
    return null;
  }

  /**
   * 在 JSON 树里找第一个**认得出来**的节点（v3.6.0）。
   *
   * 深度优先、前序 —— 外层的事件描述优先于内层的内容块。
   *
   * @param {object} node 当前节点。
   * @param {number} depth 当前深度。
   * @return {object|null} 命中的节点；没有返回 null。
   */
  function findFirstLogSignal(node, depth) {
    if (!node || typeof node !== "object" || depth > LOG_PARSE_MAX_DEPTH) {
      return null;
    }
    if (classifyLogNode(node)) {
      return node;
    }
    var keys = Object.keys(node);
    for (var i = 0; i < keys.length; i += 1) {
      var child = node[keys[i]];
      if (!child || typeof child !== "object") {
        continue;
      }
      if (Array.isArray(child)) {
        for (var j = 0; j < child.length; j += 1) {
          var hit = findFirstLogSignal(child[j], depth + 1);
          if (hit) {
            return hit;
          }
        }
        continue;
      }
      var found = findFirstLogSignal(child, depth + 1);
      if (found) {
        return found;
      }
    }
    return null;
  }

  /**
   * 在 JSON 树里定位本行事件的**信号节点**（v3.8.0）。
   *
   * 优先级：**工具调用优先**，其余类别仍是首个命中优先。
   *
   * ⚠️ 为什么要给工具调用加权重 ——
   *    Claude 同一行里 ``thinking`` 在前 + ``tool_use`` 在后时，
   *    前序遍历会先命中思考块，工具名与目标就全丢了
   *    （日志里有工具名、看板却解析不出来）。
   *    工具调用是这一行里**最该被看见**的东西，权重最高。
   *
   * @param {object} node 当前节点。
   * @param {number} depth 当前深度。
   * @return {object|null} 命中的节点；没有返回 null。
   */
  function findLogSignal(node, depth) {
    var tool = findToolSignal(node, depth);
    if (tool) {
      return tool;
    }
    return findFirstLogSignal(node, depth);
  }

  /**
   * 从命中节点里挖出正文（v3.6.0）。
   *
   * @param {object} node 命中的节点。
   * @param {number} depth 当前深度。
   * @return {string} 正文；挖不到返回空串。
   */
  function logPayloadText(node, depth) {
    if (!node || typeof node !== "object" || depth > LOG_PARSE_MAX_DEPTH) {
      return "";
    }
    var keys = [
      "text",
      "summary",
      "content",
      "message",
      "prompt",
      "thinking",
      "reasoning"
    ];
    for (var i = 0; i < keys.length; i += 1) {
      var value = node[keys[i]];
      if (value === undefined || value === null) {
        continue;
      }
      if (typeof value === "string") {
        if (value.trim()) {
          return value;
        }
        continue;
      }
      var nested = collectLogText(value, depth + 1);
      if (nested && nested.trim()) {
        return nested;
      }
    }
    return "";
  }

  /**
   * 从命中节点里挖出**工具名**（v3.6.0）。
   *
   * ⚠️ 只挖名字 —— ``arguments`` 在自然语言模式下必须藏起来，
   *    那玩意儿动辄几百个字符，正是「没法读」的元凶。
   *
   * @param {object} node 命中的节点。
   * @return {string} 工具名；认不出来返回空串。
   */
  function logToolName(node) {
    if (!node || typeof node !== "object") {
      return "";
    }
    var keys = [
      "name",
      "tool",
      "tool_name",
      "toolName",
      "function",
      "tool_call",
      "toolCall",
      "function_call",
      "functionCall"
    ];
    for (var i = 0; i < keys.length; i += 1) {
      var value = node[keys[i]];
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
      if (value && typeof value === "object") {
        var nested = value.name || value.tool_name || value.toolName;
        if (typeof nested === "string" && nested.trim()) {
          return nested.trim();
        }
      }
    }
    return "";
  }

  /* ------------------------------------- v3.7.0：动作类别与核心目标 */

  /**
   * 判断一个工具调用属于哪类**动作**（v3.7.0）。
   *
   * 只分三类 —— 读 / 写 / 跑（外加「其它」兜底）：
   * 摘要里只需要这三样，多了反而看不清。
   *
   * ⚠️ 匹配是**包含**关系而不是全等 ——
   *    真实日志里的工具名五花八门：
   *    ``Read`` / ``read_file`` / ``NotebookRead`` / ``view``……
   *    所以先归一化（小写、去掉非字母数字）再找特征词。
   *
   * @param {string} toolName 工具名。
   * @return {string} ``read`` / ``write`` / ``exec`` / ``other``。
   */
  function logActionKind(toolName) {
    var name = String(toolName || "")
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "");
    if (!name) {
      return LOG_ACTION_OTHER;
    }
    /* ⚠️ 顺序有讲究：
     *    写入类先判 —— ``search_replace_content`` 里有「search」，
     *    先判读取会被误归成「读取」。 */
    var writeWords = [
      "write",
      "edit",
      "create",
      "patch",
      "append",
      "insert",
      "delete",
      "remove",
      "update",
      "replace",
      "move",
      "rename"
    ];
    var execWords = [
      "bash",
      "shell",
      "exec",
      "run",
      "command",
      "terminal",
      "python",
      "npm",
      "node",
      "git",
      "curl",
      "wget",
      "pytest",
      "make"
    ];
    var readWords = [
      "read",
      "view",
      "cat",
      "open",
      "glob",
      "grep",
      "search",
      "list",
      "ls",
      "find",
      "fetch",
      "todoread",
      "stat"
    ];
    var groups = [
      { words: writeWords, kind: LOG_ACTION_WRITE },
      { words: execWords, kind: LOG_ACTION_EXEC },
      { words: readWords, kind: LOG_ACTION_READ }
    ];
    for (var g = 0; g < groups.length; g += 1) {
      var words = groups[g].words;
      for (var i = 0; i < words.length; i += 1) {
        if (name.indexOf(words[i]) >= 0) {
          return groups[g].kind;
        }
      }
    }
    return LOG_ACTION_OTHER;
  }

  /**
   * 从工具调用里挖出**参数**（v3.7.0）。
   *
   * 日志里的参数有三种形态：直接的对象、**字符串化的 JSON**、
   * 或者再包一层 ``function.arguments``。这里统统摊平。
   *
   * @param {object} node 命中的节点。
   * @return {object} 参数对象；挖不到返回空对象。
   */
  function logToolArguments(node) {
    if (!node || typeof node !== "object") {
      return {};
    }
    var keys = ["arguments", "parameters", "params", "args", "input"];
    for (var i = 0; i < keys.length; i += 1) {
      var value = node[keys[i]];
      if (value === undefined || value === null) {
        continue;
      }
      if (typeof value === "object") {
        return value;
      }
      if (typeof value === "string" && value.trim()) {
        try {
          var parsed = JSON.parse(value);
          if (parsed && typeof parsed === "object") {
            return parsed;
          }
        } catch (err) {
          // 不是 JSON —— 当普通字符串，继续往下找别的键。
          continue;
        }
      }
    }
    /* 再往里翻一层 —— ``{"tool":{"name":"x","arguments":{…}}}``。
     *
     * ⚠️ ``tool_call`` / ``toolCall`` 也要认 ——
     *    看板拿到的是**原始日志行**，而 mcode 的旧协议把工具调用
     *    放在 ``tool_call`` 里（``{"type":"tool_call","tool_call":{…}}``），
     *    v0.6 协议则放在 ``toolCall`` 里。不认它们，
     *    真实日志里的工具名与目标就全丢了。
     *
     * ⚠️ ``function_call`` / ``functionCall`` 同样要认 ——
     *    后端适配器认这两个键，前端不认就对不齐了。 */
    var nested = node.tool || node.function || node.call ||
      node.tool_call || node.toolCall ||
      node.function_call || node.functionCall;
    if (nested && typeof nested === "object") {
      return logToolArguments(nested);
    }
    return {};
  }

  /**
   * 把摘要里的**目标**截短（v3.7.0）。
   *
   * ⚠️ 路径与命令的截法不同：
   *    路径的信息量在**尾巴**（``src/core/auth.py`` 比 ``D:\项目\…`` 有用），
   *    命令的信息量在**开头**（``npm test`` 比 ``…--watch`` 有用）。
   *
   * @param {string} text 原始目标。
   * @param {number} limit 最多留多少字。
   * @param {boolean} keepTail 是否保留尾巴（路径用 true）。
   * @return {string} 截短后的目标。
   */
  function clipTarget(text, limit, keepTail) {
    var value = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
    if (!value) {
      return "";
    }
    if (value.length <= limit) {
      return value;
    }
    if (keepTail) {
      return "…" + value.slice(value.length - (limit - 1));
    }
    return value.slice(0, limit - 1) + "…";
  }

  /**
   * 从工具调用里挖出**核心目标**（v3.7.0）。
   *
   * ⚠️ 只挖一个目标 —— 摘要里不需要参数的全貌，
   *    也不需要把 ``arguments`` 整段搬出来。
   *
   * @param {object} node 命中的节点。
   * @param {string} action 动作类别。
   * @return {string} 核心目标；挖不到返回空串。
   */
  function logToolTarget(node, action) {
    var args = logToolArguments(node);
    if (!args || typeof args !== "object") {
      return "";
    }
    var pathKeys = [
      "file_path",
      "filePath",
      "path",
      "notebook_path",
      "absolute_path",
      "target_file",
      "filename",
      "file",
      "dir",
      "directory"
    ];
    var commandKeys = [
      "command",
      "cmd",
      "commandLine",
      "script",
      "shell_command"
    ];
    var contentKeys = ["content", "new_str", "new_string", "text", "body"];
    var kind = String(action || LOG_ACTION_OTHER);

    function firstOf(keys) {
      for (var i = 0; i < keys.length; i += 1) {
        var value = args[keys[i]];
        if (typeof value === "string" && value.trim()) {
          return value.trim();
        }
      }
      return "";
    }

    var path = firstOf(pathKeys);
    var command = firstOf(commandKeys);
    var content = firstOf(contentKeys);

    if (kind === LOG_ACTION_EXEC) {
      if (command) {
        return clipTarget(command, LOG_TARGET_LIMIT, false);
      }
      if (path) {
        return clipTarget(path, LOG_TARGET_LIMIT, true);
      }
      return "";
    }
    if (path) {
      return clipTarget(path, LOG_TARGET_LIMIT, true);
    }
    if (command) {
      return clipTarget(command, LOG_TARGET_LIMIT, false);
    }
    if (content) {
      return clipTarget(content, LOG_TARGET_LIMIT, false);
    }
    return "";
  }

  /**
   * 把一组日志条目里的**连续同类动作**合并（v3.7.0 极限摘要）。
   *
   * ⚠️ 这是纯函数 —— 不碰 DOM，所以能被自动化测试真的跑一遍。
   *
   * 合并规则：
   *   * 只合并**相邻**的条目（中间夹了别的就不算「连续」）；
   *   * 只合并**同一类动作**（读 + 读 + 读 → 一行；读 + 写 → 两行）；
   *   * 只合并**认得出目标**的条目（连读了什么都说不清，合并了更糊涂）。
   *
   * @param {Array<object>} entries 已解析、已过滤的条目数组。
   * @return {Array<object>} 合并后的条目数组。
   */
  function mergeLogEntries(entries) {
    var list = Array.isArray(entries) ? entries : [];
    var merged = [];
    var index = 0;
    while (index < list.length) {
      var entry = list[index];
      var mergeable =
        entry &&
        entry.kind === "tool" &&
        entry.action &&
        entry.action !== LOG_ACTION_OTHER &&
        entry.target;
      if (!mergeable) {
        merged.push(entry);
        index += 1;
        continue;
      }
      var group = [entry];
      var next = index + 1;
      while (next < list.length) {
        var candidate = list[next];
        if (
          !candidate ||
          candidate.kind !== "tool" ||
          candidate.action !== entry.action ||
          !candidate.target
        ) {
          break;
        }
        group.push(candidate);
        next += 1;
      }
      if (group.length > 1) {
        merged.push({
          kind: "action",
          action: entry.action,
          target: entry.target,
          count: group.length,
          index: entry.index
        });
      } else {
        merged.push(entry);
      }
      index = next;
    }
    return merged;
  }

  /**
   * 把一个（可能已合并的）动作条目写成一句人话（v3.7.0）。
   *
   * 格式：``[读取] core/auth.py`` /
   *        ``[读取] core/auth.py 等 5 个文件`` /
   *        ``[执行] npm test 等 3 条命令``。
   *
   * @param {object} entry 条目。
   * @return {string} 摘要文本。
   */
  function logActionText(entry) {
    var source = entry || {};
    var label = LOG_ACTION_LABELS[source.action] || LOG_ACTION_LABELS.other;
    var target = String(source.target || "");
    var count = Number(source.count) || 1;
    if (count <= 1) {
      return "[" + label + "] " + target;
    }
    var unit = source.action === LOG_ACTION_EXEC ? "条命令" : "个文件";
    return "[" + label + "] " + target + " 等 " + count + " " + unit;
  }

  /**
   * 解析一行日志（v3.6.0）。
   *
   * ⚠️ v3.7.0 起多带两个字段：
   *    ``action`` —— 工具调用的动作类别（读 / 写 / 跑）；
   *    ``target`` —— 工具调用的核心目标（路径或命令）。
   *    极限摘要靠它们把连续同类动作**真的合并**成一行。
   *
   * @param {string} raw 原始行。
   * @return {{kind: string, text: string, tool: string, noise: boolean,
   *          action: string, target: string}}
   *   类别、正文、工具名、是否整行都是噪声、动作类别、核心目标。
   */
  function parseLogEntry(raw) {
    var source = String(raw == null ? "" : raw);
    var trimmed = source.trim();
    if (!trimmed) {
      return {
        kind: "plain",
        text: "",
        tool: "",
        noise: false,
        action: "",
        target: ""
      };
    }
    var data = null;
    if (trimmed.charAt(0) === "{" || trimmed.charAt(0) === "[") {
      try {
        data = JSON.parse(trimmed);
      } catch (err) {
        // 不是合法 JSON —— 原样留着，别假装解析成功了。
        data = null;
      }
    }
    if (!data || typeof data !== "object") {
      return {
        kind: "plain",
        text: source,
        tool: "",
        noise: false,
        action: "",
        target: ""
      };
    }
    var node = findLogSignal(data, 0);
    if (!node) {
      // 合法 JSON，但认不出事件类型 —— 归到「其他」。
      return {
        kind: "other",
        text: "",
        tool: "",
        noise: false,
        action: "",
        target: ""
      };
    }
    var kind = classifyLogNode(node);
    var body = logPayloadText(node, 0);
    var clean = stripNoiseSections(body);
    var noise = !!body && !clean;
    var tool = kind === "tool" ? logToolName(node) : "";
    if (kind === "tool" && !tool) {
      tool = "未命名工具";
    }
    var action = "";
    var target = "";
    if (kind === "tool") {
      action = logActionKind(tool);
      target = logToolTarget(node, action);
    }
    if (!clean && kind !== "tool") {
      return {
        kind: "other",
        text: "",
        tool: tool,
        noise: noise,
        action: action,
        target: target
      };
    }
    return {
      kind: kind,
      text: clean,
      tool: tool,
      noise: noise,
      action: action,
      target: target
    };
  }

  /**
   * 档位落在哪一段（v3.7.0：四段）。
   *
   * @param {number} scale 档位（0-100）。
   * @return {string} ``code`` / ``natural`` / ``summary`` / ``extreme``。
   */
  function logScaleMode(scale) {
    var value = Number(scale);
    if (!isFinite(value)) {
      value = LOG_SCALE_DEFAULT;
    }
    /* ⚠️ 极限摘要**必须先判** ——
     *    100 既是「高度摘要」的上界，也是「极限摘要」的下界。 */
    if (value >= LOG_SCALE_EXTREME) {
      return "extreme";
    }
    if (value >= LOG_SCALE_SUMMARY_MIN) {
      return "summary";
    }
    if (value <= LOG_SCALE_CODE_MAX) {
      return "code";
    }
    return "natural";
  }

  /**
   * 档位的中文名（v3.7.0）。
   *
   * @param {number} scale 档位（0-100）。
   * @return {string} 中文档位名。
   */
  function logScaleLabel(scale) {
    var mode = logScaleMode(scale);
    if (mode === "extreme") {
      return "极限摘要";
    }
    if (mode === "code") {
      return "纯代码";
    }
    if (mode === "summary") {
      return "高度摘要";
    }
    return "自然语言";
  }

  /**
   * 某一类事件前面的标签（v3.6.0）。
   *
   * @param {string} kind 事件类别。
   * @return {string} 标签文本。
   */
  function logKindTag(kind) {
    if (kind === "user") {
      return "🧑 用户提问：";
    }
    if (kind === "reply") {
      return "🤖 Agent 回答：";
    }
    if (kind === "think") {
      return "💭 思考：";
    }
    if (kind === "tool") {
      return "🔧 工具调用：";
    }
    if (kind === "tool_result") {
      return "📦 工具结果：";
    }
    if (kind === "result") {
      return "✅ 本轮完成：";
    }
    if (kind === "context") {
      return "📋 上下文摘要：";
    }
    if (kind === "other") {
      return "· 其他";
    }
    return "";
  }

  /**
   * 某一类事件在这一档位下**要不要显示**（v3.6.0）。
   *
   * @param {object} parsed 解析结果。
   * @param {string} mode 当前档位。
   * @return {boolean} 要显示返回 ``true``。
   */
  function logRowVisible(parsed, mode) {
    if (!parsed) {
      return false;
    }
    if (mode === "code") {
      // 纯代码档位展示的是**原始行**本身 ——
      // 噪声也好、工具结果也好，一行都不少。
      return true;
    }
    if (parsed.noise) {
      return false;
    }
    if (mode === "summary" || mode === "extreme") {
      // 高度摘要 / 极限摘要：只留对话主干 —— 问题、回答、核心工具调用。
      return (
        parsed.kind === "user" ||
        parsed.kind === "reply" ||
        parsed.kind === "tool" ||
        parsed.kind === "result"
      );
    }
    // 自然语言：工具结果与冗长参数一律藏起来。
    return parsed.kind !== "tool_result";
  }

  /**
   * 某一类事件在这一档位下**显示什么**（v3.6.0）。
   *
   * @param {object} parsed 解析结果。
   * @param {string} mode 当前档位。
   * @return {string} 正文文本。
   */
  function logRowBody(parsed, mode) {
    var extreme = mode === "extreme";
    var limit =
      extreme || mode === "summary"
        ? LOG_SUMMARY_LIMIT
        : LOG_NATURAL_LIMIT;
    if (!parsed) {
      return "";
    }
    if (parsed.kind === "tool") {
      // ⚠️ 只给工具名 —— arguments 在这里必须消失。
      return parsed.tool || "未命名工具";
    }
    if (parsed.kind === "other") {
      return "";
    }
    var text = parsed.text || "";
    if (extreme) {
      /* ⚠️ v3.9.0：极限摘要必须是**新闻快讯**，不是代码审查报告。
       *
       *   ① 先把 ``` 代码块**彻底剥离** ——
       *      Agent 贴一段 diff 进来时，用户要看到的是
       *      「他贴了段代码」，不是那段代码本身；
       *   ② 长文本**只取前两句** ——
       *      一句快讯说清「刚才发生了什么」就够了。
       *
       * ⚠️ 只在极限档生效 —— 纯代码档（0）本来就要看全文。 */
      text = firstSentences(
        stripLogCodeBlocks(text),
        LOG_EXTREME_SENTENCES
      );
    }
    if (parsed.kind === "result") {
      return text ? clipText(text, limit) : "本轮任务完成。";
    }
    return clipText(text, limit);
  }

  /**
   * 把一段 JSON 文本**逐词**切开（v3.6.0）。
   *
   * ⚠️ 纯函数 —— 不碰 DOM，所以能被自动化测试真的跑一遍。
   *
   * @param {string} text 原始文本。
   * @return {Array<{type: string, text: string}>} 词元列表。
   */
  function logJsonTokens(text) {
    var source = String(text == null ? "" : text);
    var tokens = [];
    var pattern = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}[\],:])/g;
    var last = 0;
    var match = pattern.exec(source);
    while (match) {
      if (match.index > last) {
        tokens.push({ type: "plain", text: source.slice(last, match.index) });
      }
      if (typeof match[1] === "string") {
        if (typeof match[2] === "string") {
          tokens.push({ type: "key", text: match[1] });
          tokens.push({ type: "plain", text: match[2] });
        } else {
          tokens.push({ type: "string", text: match[1] });
        }
      } else if (typeof match[3] === "string") {
        tokens.push({ type: "number", text: match[3] });
      } else if (typeof match[4] === "string") {
        tokens.push({ type: "bool", text: match[4] });
      } else if (typeof match[5] === "string") {
        tokens.push({ type: "punct", text: match[5] });
      }
      last = pattern.lastIndex;
      match = pattern.exec(source);
    }
    if (last < source.length) {
      tokens.push({ type: "plain", text: source.slice(last) });
    }
    return tokens;
  }

  /**
   * 把一段 JSON 文本**逐词**高亮（v3.6.0）。
   *
   * ⚠️ 不用 ``innerHTML`` —— 日志内容是**外部输入**，
   *    拼字符串进 HTML 等于开门揖盗。
   *    这里全程用 ``createTextNode`` + ``createElement``。
   *
   * @param {string} text 原始文本。
   * @return {DocumentFragment} 高亮后的片段。
   */
  function renderLogJson(text) {
    var fragment = document.createDocumentFragment();
    var tokens = logJsonTokens(text);
    for (var i = 0; i < tokens.length; i += 1) {
      var token = tokens[i];
      if (token.type === "plain") {
        fragment.appendChild(document.createTextNode(token.text));
        continue;
      }
      var span = document.createElement("span");
      span.className = "log-json__" + token.type;
      span.textContent = token.text;
      fragment.appendChild(span);
    }
    return fragment;
  }

  /**
   * 造出一行日志（v3.6.0）。
   *
   * @param {number} index 这一行在原始日志里的序号。
   * @param {string} raw 原始行文本。
   * @param {string} mode 当前档位；不传就现读状态。
   * @return {HTMLElement|null} 行元素；这一档位下不显示返回 null。
   */
  function buildLogRow(index, raw, mode) {
    var current = mode || logScaleMode(state.logCompression);
    var parsed = parseLogEntry(raw);
    if (!logRowVisible(parsed, current)) {
      return null;
    }
    var row = document.createElement("div");
    row.className = "log-line";
    row.setAttribute("data-raw-index", String(index));
    var no = document.createElement("span");
    no.className = "log-line__no";
    no.textContent = String(index + 1);
    row.appendChild(no);
    var body = document.createElement("span");
    body.className = "log-line__text";
    if (current === "code") {
      body.classList.add("log-line__text--json");
      body.appendChild(renderLogJson(raw));
      row.appendChild(body);
      return row;
    }
    row.classList.add("log-line--nl");
    if (parsed.kind === "think") {
      row.classList.add("log-line--think");
    }
    var tag = document.createElement("span");
    tag.className = "log-line__tag log-line__tag--" + parsed.kind;
    tag.textContent = logKindTag(parsed.kind);
    body.appendChild(tag);
    var content = document.createElement("span");
    content.className = "log-line__body";
    content.textContent = logRowBody(parsed, current);
    body.appendChild(content);
    row.appendChild(body);
    return row;
  }

  /**
   * 造出一行**合并后的动作摘要**（v3.7.0 极限摘要）。
   *
   * 这一行代表**一串**连续动作 —— 所以序号取这一串的**第一行**，
   * 后面那几行的原始内容都不再单独出现。
   *
   * @param {object} entry 合并后的条目。
   * @return {HTMLElement} 行元素。
   */
  function buildActionRow(entry) {
    var source = entry || {};
    var row = document.createElement("div");
    row.className = "log-line log-line--action";
    if (source.index !== undefined && source.index !== null) {
      row.setAttribute("data-raw-index", String(source.index));
    }
    var no = document.createElement("span");
    no.className = "log-line__no";
    no.textContent = String(Number(source.index || 0) + 1);
    row.appendChild(no);
    var body = document.createElement("span");
    body.className = "log-line__text";
    var tag = document.createElement("span");
    tag.className =
      "log-line__tag log-line__tag--action log-line__tag--action-" +
      String(source.action || LOG_ACTION_OTHER);
    tag.textContent = "⚡ 极限摘要：";
    body.appendChild(tag);
    var content = document.createElement("span");
    content.className = "log-line__body";
    content.textContent = logActionText(source);
    body.appendChild(content);
    row.appendChild(body);
    return row;
  }

  /**
   * 用缓存的原始行**重画**整块日志（v3.6.0，v3.7.0 加极限摘要）。
   *
   * ⚠️ 必须**一次性**换掉所有子节点（DocumentFragment）——
   *    一行一行地换会让浏览器重排几百次，用户看到的是「闪烁」。
   *
   * @return {void}
   */
  function redrawLogLines() {
    if (!el.logLines) {
      return;
    }
    var box = logScrollBox();
    var keep = box ? box.scrollTop : 0;
    var follow = state.logFollow;
    var mode = logScaleMode(state.logCompression);
    var rows = [];
    if (mode === "extreme") {
      /* 极限摘要：先把可见条目收成序列，
       * 再把**连续同类动作**合并成一行 —— 这就是「真合并」。 */
      var entries = [];
      for (var i = 0; i < state.logRaw.length; i += 1) {
        var parsed = parseLogEntry(state.logRaw[i]);
        if (!logRowVisible(parsed, mode)) {
          continue;
        }
        entries.push({
          index: state.logRawBase + i,
          raw: state.logRaw[i],
          kind: parsed.kind,
          text: parsed.text,
          tool: parsed.tool,
          action: parsed.action,
          target: parsed.target
        });
      }
      var merged = mergeLogEntries(entries);
      for (var j = 0; j < merged.length; j += 1) {
        var item = merged[j];
        if (item && item.kind === "action") {
          rows.push(buildActionRow(item));
          continue;
        }
        var row = buildLogRow(item.index, item.raw, mode);
        if (row) {
          rows.push(row);
        }
      }
    } else {
      for (var k = 0; k < state.logRaw.length; k += 1) {
        var normal = buildLogRow(state.logRawBase + k, state.logRaw[k], mode);
        if (normal) {
          rows.push(normal);
        }
      }
    }
    while (el.logLines.firstElementChild) {
      el.logLines.removeChild(el.logLines.firstElementChild);
    }
    var fragment = document.createDocumentFragment();
    for (var n = 0; n < rows.length; n += 1) {
      fragment.appendChild(rows[n]);
    }
    el.logLines.appendChild(fragment);
    if (!rows.length && el.logEmpty && !el.logEmpty.parentNode) {
      el.logLines.appendChild(el.logEmpty);
    }
    if (!box) {
      return;
    }
    if (follow) {
      box.scrollTop = box.scrollHeight;
    } else {
      box.scrollTop = keep;
    }
  }

  /**
   * 把一个档位夹进 0-100（v3.7.0）。
   *
   * @param {*} raw 原始值。
   * @param {number} fallback 读不出来时的兜底。
   * @return {number} 合法档位。
   */
  function clampCompression(raw, fallback) {
    var parsed = Number(raw);
    if (!isFinite(parsed)) {
      return fallback;
    }
    return Math.min(LOG_SCALE_MAX, Math.max(LOG_SCALE_MIN, Math.round(parsed)));
  }

  /**
   * 应用一整套**呈现档位**（v3.7.0）。
   *
   * ⚠️ 档位来自服务端（``/api/presentation``）——
   *    设置页改了以后，所有看板窗口都会跟着变。
   *
   * @param {object} payload 档位载荷。
   * @return {void}
   */
  function applyPresentation(payload) {
    var data = payload || {};
    var logLevel = clampCompression(
      data.log_compression,
      LOG_SCALE_DEFAULT
    );
    var auditLevel = clampCompression(
      data.audit_compression,
      AUDIT_SCALE_DEFAULT
    );
    var changed =
      logLevel !== state.logCompression ||
      auditLevel !== state.auditCompression;
    state.logCompression = logLevel;
    state.auditCompression = auditLevel;
    if (!changed) {
      return;
    }
    redrawLogLines();
    renderLiveAudit(state.liveAuditAll || []);
  }

  /**
   * 读一条本地存储（v3.6.0）。
   *
   * ⚠️ 浏览器隐私模式 / 测试沙箱里 ``localStorage`` 可能整个不存在，
   *    所以这里一律容错 —— 读不到就当没有，绝不抛异常。
   *
   * @param {string} key 存储键。
   * @return {string|null} 存储值；读不到返回 null。
   */
  function readStoredValue(key) {
    try {
      var storage = window.localStorage;
      if (!storage || typeof storage.getItem !== "function") {
        return null;
      }
      return storage.getItem(key);
    } catch (err) {
      console.warn("读取本地存储失败：", err);
      return null;
    }
  }

  /**
   * 写一条本地存储（v3.6.0）。
   *
   * @param {string} key 存储键。
   * @param {string} value 存储值。
   * @return {void}
   */
  function writeStoredValue(key, value) {
    try {
      var storage = window.localStorage;
      if (!storage || typeof storage.setItem !== "function") {
        return;
      }
      storage.setItem(key, value);
    } catch (err) {
      console.warn("写入本地存储失败：", err);
    }
  }

  /**
   * 从服务端读呈现档位（v3.7.0）。
   *
   * ⚠️ 读失败**沿用当前档位** ——
   *    配置接口抽风不该把看板搞坏，更不该把日志清空。
   *
   * @return {Promise} 读档位的 Promise。
   */
  function loadPresentation() {
    return fetch("/api/presentation")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          applyPresentation(data);
        }
        return data;
      })
      .catch(function () {
        return null;
      });
  }

  /* --------------------------------------- 多面板拖拽与折叠（v3.6.0） */
  //
  // v3.5.2 的布局是**写死**的 Grid：三列 248px / 1fr / 30%，
  // 中间面板的告警与流水还各占死高度。
  // 用户想「多看告警」「多看日志」都没辙。
  //
  // v3.6.0 引入**真的**拖拽分隔条（不是伪元素 ——
  // 伪元素拿不到指针事件，拖不动）：
  //   左右拖 → 调会话栏 / 被监控日志的宽度
  //   上下拖 → 调告警区 / 流水区的比例
  //   折叠   → 会话栏收成一条极窄的侧边条
  // 拖出来的比例存 localStorage —— 刷新页面还是上次的手感。
  //
  // ⚠️ 宽度**只由 CSS 变量驱动**（--sessions-w / --stream-w / --audit-h），
  //    JS 只改变量 —— 排版规则只有 CSS 一份解释者，
  //    免得 JS 与 CSS 各写一套、互相打脸。

  /**
   * 给一个元素设置 CSS 变量（v3.6.0）。
   *
   * @param {HTMLElement} node 目标元素。
   * @param {string} name 变量名（含 --）。
   * @param {string|null} value 值；传 null 表示**删掉**（退回样式表）。
   * @return {void}
   */
  function setCssVar(node, name, value) {
    if (!node || !node.style || typeof node.style.setProperty !== "function") {
      return;
    }
    if (value === null) {
      if (typeof node.style.removeProperty === "function") {
        node.style.removeProperty(name);
      }
      return;
    }
    node.style.setProperty(name, value);
  }

  /**
   * 量一量布局的**实际**尺寸（v3.6.0）。
   *
   * ⚠️ 必须量 DOM，不能读 CSS 变量 ——
   *    ``--stream-w`` 的默认值是 ``30%``，解析出来是 30 而不是像素。
   *
   * @return {{width: number, resizer: number, gap: number, panels: number}}
   *   容器宽度、分隔条宽度、列间距、三个面板可分的总宽。
   */
  function layoutMetrics() {
    var width = 0;
    var resizer = 8;
    var gap = 14;
    var body = el.bodyLayout;
    if (body && body.getBoundingClientRect) {
      var rect = body.getBoundingClientRect();
      width = (rect && rect.width) || 0;
      if (typeof window.getComputedStyle === "function") {
        var style = window.getComputedStyle(body);
        var parsedResizer = parseFloat(style.getPropertyValue("--resizer-w"));
        if (isFinite(parsedResizer) && parsedResizer > 0) {
          resizer = parsedResizer;
        }
        var parsedGap = parseFloat(style.columnGap || style.gap);
        if (isFinite(parsedGap) && parsedGap > 0) {
          gap = parsedGap;
        }
      }
    }
    // 五列 + 四道间距 + 两条分隔条。
    var panels = Math.max(0, width - resizer * 2 - gap * 4);
    return { width: width, resizer: resizer, gap: gap, panels: panels };
  }

  /**
   * 会话栏宽度的合法区间（v3.6.0）。
   *
   * @param {number} value 期望宽度（像素）。
   * @return {number} 夹紧后的宽度。
   */
  function clampSessions(value) {
    var metrics = layoutMetrics();
    var max = LAYOUT_MAX_SESSIONS;
    var byRoom = metrics.panels - LAYOUT_MIN_STREAM - LAYOUT_MIN_ALERTS;
    if (byRoom < max) {
      max = byRoom;
    }
    if (max < LAYOUT_MIN_SESSIONS) {
      max = LAYOUT_MIN_SESSIONS;
    }
    var number = Number(value);
    if (!isFinite(number)) {
      number = LAYOUT_MIN_SESSIONS;
    }
    return Math.min(max, Math.max(LAYOUT_MIN_SESSIONS, number));
  }

  /**
   * 被监控日志宽度的合法区间（v3.6.0）。
   *
   * @param {number} value 期望宽度（像素）。
   * @return {number} 夹紧后的宽度。
   */
  function clampStream(value) {
    var metrics = layoutMetrics();
    var max = metrics.panels - LAYOUT_MIN_SESSIONS - LAYOUT_MIN_ALERTS;
    if (max < LAYOUT_MIN_STREAM) {
      max = LAYOUT_MIN_STREAM;
    }
    var number = Number(value);
    if (!isFinite(number)) {
      number = LAYOUT_MIN_STREAM;
    }
    return Math.min(max, Math.max(LAYOUT_MIN_STREAM, number));
  }

  /**
   * 流水区高度的合法区间（v3.6.0）。
   *
   * @param {number} value 期望高度（像素）。
   * @return {number} 夹紧后的高度。
   */
  function clampAudit(value) {
    var max = LAYOUT_MIN_AUDIT;
    if (el.alertPanel && el.alertPanel.getBoundingClientRect) {
      var rect = el.alertPanel.getBoundingClientRect();
      var height = (rect && rect.height) || 0;
      // 告警区至少留 120px，标题栏与分隔条约 48px。
      var room = height - LAYOUT_MIN_ALERT_AREA - 48;
      if (isFinite(room) && room > max) {
        max = room;
      }
    }
    var number = Number(value);
    if (!isFinite(number)) {
      number = LAYOUT_MIN_AUDIT;
    }
    return Math.min(max, Math.max(LAYOUT_MIN_AUDIT, number));
  }

  /**
   * 量当前会话栏宽度（v3.6.0）。
   *
   * @return {number} 宽度（像素）。
   */
  function currentSessionsWidth() {
    if (el.sessionPanel && el.sessionPanel.getBoundingClientRect) {
      var rect = el.sessionPanel.getBoundingClientRect();
      if (rect && rect.width) {
        return rect.width;
      }
    }
    return state.layout.sessions || 0;
  }

  /**
   * 量当前被监控日志宽度（v3.6.0）。
   *
   * @return {number} 宽度（像素）。
   */
  function currentStreamWidth() {
    if (el.streamPanel && el.streamPanel.getBoundingClientRect) {
      var rect = el.streamPanel.getBoundingClientRect();
      if (rect && rect.width) {
        return rect.width;
      }
    }
    return state.layout.stream || 0;
  }

  /**
   * 量当前流水区高度（v3.6.0）。
   *
   * @return {number} 高度（像素）。
   */
  function currentAuditHeight() {
    if (el.liveAuditBox && el.liveAuditBox.getBoundingClientRect) {
      var rect = el.liveAuditBox.getBoundingClientRect();
      if (rect && rect.height) {
        return rect.height;
      }
    }
    return state.layout.audit || 0;
  }

  /**
   * 把当前布局写进 CSS 变量（v3.6.0）。
   *
   * @param {boolean} persist 是否顺手存进 localStorage。
   * @return {void}
   */
  function applyLayout(persist) {
    var layout = el.bodyLayout;
    if (!layout) {
      return;
    }
    var sessions = clampSessions(state.layout.sessions);
    var stream = clampStream(state.layout.stream);
    var audit = clampAudit(state.layout.audit);
    state.layout.sessions = sessions;
    state.layout.stream = stream;
    state.layout.audit = audit;
    // ⚠️ 折叠态下**不能**写 --sessions-w ——
    //   内联样式的优先级高于样式表里的折叠规则，
    //   写了就等于把「46px 窄条」顶掉，会话栏根本收不起来。
    if (state.layout.collapsed) {
      setCssVar(layout, "--sessions-w", null);
    } else {
      setCssVar(layout, "--sessions-w", sessions + "px");
    }
    setCssVar(layout, "--stream-w", stream + "px");
    if (el.alertPanel) {
      setCssVar(el.alertPanel, "--audit-h", audit + "px");
    }
    if (persist !== false) {
      saveLayout();
    }
  }

  /**
   * 保存布局到 localStorage（v3.6.0）。
   *
   * @return {void}
   */
  function saveLayout() {
    writeStoredValue(
      LAYOUT_STORAGE_KEY,
      JSON.stringify({
        sessions: Math.round(state.layout.sessions),
        stream: Math.round(state.layout.stream),
        audit: Math.round(state.layout.audit),
        collapsed: !!state.layout.collapsed
      })
    );
  }

  /**
   * 读回上次保存的布局（v3.6.0）。
   *
   * @return {object|null} 布局数据；没有或坏了返回 null。
   */
  function loadLayoutState() {
    var raw = readStoredValue(LAYOUT_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    try {
      var data = JSON.parse(raw);
      if (!data || typeof data !== "object") {
        return null;
      }
      return data;
    } catch (err) {
      console.warn("解析已保存的布局失败：", err);
      return null;
    }
  }

  /**
   * 收起 / 展开会话栏（v3.6.0）。
   *
   * @param {boolean} collapsed 是否收起。
   * @param {boolean} persist 是否存进 localStorage。
   * @return {void}
   */
  function setSessionsCollapsed(collapsed, persist) {
    var next = !!collapsed;
    state.layout.collapsed = next;
    var layout = el.bodyLayout;
    if (layout && layout.classList && layout.classList.toggle) {
      layout.classList.toggle("is-sessions-collapsed", next);
    }
    if (el.sessionsRail) {
      el.sessionsRail.hidden = !next;
    }
    if (el.sessionsCollapse) {
      el.sessionsCollapse.setAttribute("aria-expanded", next ? "false" : "true");
      el.sessionsCollapse.textContent = next ? "⟩" : "⟨";
    }
    updateSessionsRailCount();
    // ⚠️ v3.6.0：收起时必须**摘掉内联的 --sessions-w** ——
    //   拖拽之后 applyLayout() 会把宽度写成内联样式，
    //   而内联的优先级**高于**样式表里的折叠规则
    //   （.body.is-sessions-collapsed { --sessions-w: 46px }），
    //   不摘的话左边会留下一块 368px 的空白，压根收不起来。
    //   applyLayout() 本身就认得折叠态：收起时它只负责「摘」，
    //   展开时再把用户上次拖出来的宽度写回去。
    applyLayout(false);
    if (persist !== false) {
      saveLayout();
    }
  }

  /**
   * 刷新折叠窄条上的会话数量（v3.6.0）。
   *
   * @return {void}
   */
  function updateSessionsRailCount() {
    if (!el.sessionsRailCount) {
      return;
    }
    var count = (state.sessions || []).filter(function (item) {
      return item && !item.placeholder && String(item.id) !== "radar";
    }).length;
    el.sessionsRailCount.textContent = String(count);
  }

  /**
   * 给一条分隔条接上拖拽（v3.6.0）。
   *
   * @param {HTMLElement} node 分隔条元素。
   * @param {string} mode ``sessions`` / ``stream`` / ``audit``。
   * @return {void}
   */
  function bindResizer(node, mode) {
    if (!node || typeof node.addEventListener !== "function") {
      return;
    }
    node.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) {
        return;
      }
      event.preventDefault();
      node.classList.add("is-dragging");
      if (document.body && document.body.classList) {
        document.body.classList.add("is-resizing");
      }
      var startX = event.clientX || 0;
      var startY = event.clientY || 0;
      var startSessions = currentSessionsWidth();
      var startStream = currentStreamWidth();
      var startAudit = currentAuditHeight();
      /**
       * 拖动过程中的实时计算。
       *
       * @param {Event} moveEvent 指针移动事件。
       * @return {void}
       */
      var move = function (moveEvent) {
        var dx = (moveEvent.clientX || 0) - startX;
        var dy = (moveEvent.clientY || 0) - startY;
        if (mode === "sessions") {
          state.layout.sessions = clampSessions(startSessions + dx);
        } else if (mode === "stream") {
          // 往左拖 → 日志面板变**宽**。
          state.layout.stream = clampStream(startStream - dx);
        } else if (mode === "audit") {
          // 往下拖 → 流水区变**高**。
          //
          // ⚠️ v3.6.1：这里的符号**一直是对的**（`+ dy`）——
          //    反向的锅在**布局**上：
          //    v3.6.0 把流水区放在分隔条**下面**，
          //    而它又是面板的最后一个 flex 子项、被面板底边钉死，
          //    于是「分隔条 Y = 面板底边 − 流水高」，导数是 −1 ——
          //    流水长高 1px，分隔条就被顶上去 1px，正好与鼠标相反。
          //    现在流水区挪到分隔条**上方**，
          //    「分隔条 Y = 面板顶边 + 流水高」，导数是 +1，
          //    把手这才**跟得上鼠标**（与左右两条分隔条同构）。
          state.layout.audit = clampAudit(startAudit + dy);
        }
        applyLayout(false);
      };
      /**
       * 松手收摊。
       *
       * @return {void}
       */
      var stop = function () {
        node.classList.remove("is-dragging");
        if (document.body && document.body.classList) {
          document.body.classList.remove("is-resizing");
        }
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
        saveLayout();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
      window.addEventListener("pointercancel", stop);
    });
  }

  /**
   * 窗口尺寸变了 → 重新夹一次（v3.6.0）。
   *
   * @return {void}
   */
  function onWindowResize() {
    applyLayout(false);
  }

  /**
   * 初始化多面板拖拽与折叠（v3.6.0）。
   *
   * @return {void}
   */
  function initLayout() {
    var saved = loadLayoutState();
    if (saved) {
      state.layout.sessions = Number(saved.sessions) || 0;
      state.layout.stream = Number(saved.stream) || 0;
      state.layout.audit = Number(saved.audit) || 0;
      state.layout.collapsed = !!saved.collapsed;
    }
    bindResizer(el.resizerSessions, "sessions");
    bindResizer(el.resizerStream, "stream");
    bindResizer(el.resizerAudit, "audit");
    if (el.sessionsCollapse) {
      el.sessionsCollapse.addEventListener("click", function () {
        setSessionsCollapsed(!state.layout.collapsed, true);
      });
    }
    if (el.sessionsExpand) {
      el.sessionsExpand.addEventListener("click", function () {
        setSessionsCollapsed(false, true);
      });
    }
    // 用户还没拖过时，用**当前实际尺寸**当起点。
    if (!state.layout.sessions) {
      state.layout.sessions = currentSessionsWidth();
    }
    if (!state.layout.stream) {
      state.layout.stream = currentStreamWidth();
    }
    if (!state.layout.audit) {
      state.layout.audit = currentAuditHeight();
    }
    applyLayout(false);
    setSessionsCollapsed(state.layout.collapsed, false);
    updateSessionsRailCount();
    window.addEventListener("resize", onWindowResize);
  }

  /* -------------------------------- 移动端沉浸式 Tab 栏（v3.9.0） */
  //
  // v3.0.0 的窄屏版式是「上下流式堆叠」—— 告警、流水、日志
  // 一起往下滚。可 390×844 的手机上这三块加起来远超一屏，
  // 用户想看告警得先滑过流水和日志。
  //
  // v3.9.0 改成**底部 Tab**：同一时间屏幕上只占满一个功能区块。
  // 显隐规则在 CSS（≤768px 媒体查询内），JS 只负责三件事：
  //   ① 切换 body 上的 data-mobile-tab；
  //   ② 把选择记进 localStorage（下次打开还在同一个 Tab）；
  //   ③ 同步 aria-selected，让读屏软件知道自己在哪。
  //
  // ⚠️ 桌面端**完全不受影响** —— 显隐规则全在媒体查询里，
  //    body 上带着 data-mobile-tab 也无所谓。

  /** 移动端 Tab 的存储键（命名约定：sentinel.<主题>.v<版本>）。 */
  var MOBILE_TAB_STORAGE_KEY = "sentinel.mobileTab.v1";

  /** 合法的 Tab 名 —— 白名单，防止 localStorage 被写脏后整页空白。 */
  var MOBILE_TABS = ["alerts", "audit", "log", "settings"];

  /** 兜底 Tab —— 认不出来时的默认值。 */
  var MOBILE_TAB_DEFAULT = "alerts";

  /**
   * 读出上次停留的 Tab。
   *
   * @return {string} 合法 Tab 名。
   */
  function readMobileTab() {
    var raw = readStoredValue(MOBILE_TAB_STORAGE_KEY);
    if (raw && MOBILE_TABS.indexOf(raw) >= 0) {
      return raw;
    }
    return MOBILE_TAB_DEFAULT;
  }

  /**
   * 切换到某个 Tab。
   *
   * @param {string} name Tab 名（不在白名单里就退回默认）。
   * @param {boolean} persist 是否记住这次选择（默认记住）。
   * @return {string} 实际生效的 Tab 名。
   */
  function applyMobileTab(name, persist) {
    var target = MOBILE_TABS.indexOf(name) >= 0 ? name : MOBILE_TAB_DEFAULT;
    document.body.setAttribute("data-mobile-tab", target);
    if (el.mobileTabs) {
      var items = el.mobileTabs.querySelectorAll("[data-mobile-tab]");
      Array.prototype.forEach.call(items, function (item) {
        var active = item.getAttribute("data-mobile-tab") === target;
        item.setAttribute("aria-selected", active ? "true" : "false");
      });
    }
    if (persist !== false) {
      writeStoredValue(MOBILE_TAB_STORAGE_KEY, target);
    }
    return target;
  }

  /**
   * 初始化移动端 Tab 栏。
   *
   * @return {void}
   */
  function initMobileTabs() {
    if (el.mobileTabs) {
      el.mobileTabs.addEventListener("click", function (event) {
        var node = event.target;
        while (node && node !== document) {
          if (node.getAttribute && node.getAttribute("data-mobile-tab")) {
            applyMobileTab(node.getAttribute("data-mobile-tab"), true);
            return;
          }
          node = node.parentNode;
        }
      });
    }
    applyMobileTab(readMobileTab(), false);
  }

  /* -------------------------------------------- 流水自然语言（v3.5.1） */
  //
  // v3.5.0 的流水全是「正常读取: auth.py」这种**机器腔**，
  // 用户根本不知道 Agent 到底在干嘛。
  // v3.5.1 把它翻译成**一句人话**，并按严重程度分成三色：
  //   绿   —— 正常运行
  //   橘黄 —— 可能有问题
  //   红   —— 严重问题

  /**
   * 把一段文字压成前 N 个字符（v3.5.1）。
   *
   * @param {string} text 原文。
   * @param {number} limit 最多保留多少字。
   * @return {string} 压短后的单行文本。
   */
  function clipText(text, limit) {
    var flat = String(text || "").replace(/\s+/g, " ").trim();
    if (flat.length <= limit) {
      return flat;
    }
    // ⚠️ 切完再 trim 一下 —— 免得留下一个悬空的空格再接省略号。
    return flat.slice(0, Math.max(limit - 1, 1)).trim() + "…";
  }

  /**
   * 从后端摘要里取出中文动词（``正常读取: auth.py`` → ``读取``）。
   *
   * @param {string} summary 后端摘要。
   * @return {string} 动词；认不出来时返回空串。
   */
  function auditVerb(summary) {
    var text = String(summary || "").trim();
    if (text.indexOf("正常") !== 0) {
      return "";
    }
    var rest = text.slice(2);
    var index = rest.indexOf(":");
    if (index <= 0) {
      return "";
    }
    return rest.slice(0, index);
  }

  /**
   * 这条流水是不是一条**告警**（v3.5.1）。
   *
   * 告警有两种来源：
   *
   * * SSE 实时推来的 —— 靠 ``type == "alert"`` 认；
   * * 服务端快照里的告警流水 —— 靠 ``stream_kind == "alert"`` 认。
   *
   * ⚠️ 告警自己还有个 ``event_type``（触发它的那次事件类型，
   * 比如 ``tool_call``）—— **不能**拿它当判据，
   * 否则「tool_call 触发的告警」会被当成普通流水。
   *
   * @param {object} entry 流水条目。
   * @return {boolean} 是告警返回 ``true``。
   */
  function isAuditAlert(entry) {
    var source = entry || {};
    return (
      String(source.stream_kind || "") === "alert" ||
      String(source.type || "") === "alert"
    );
  }

  /**
   * 把一条流水条目翻译成「一句人话」+ 一个颜色等级（v3.5.1）。
   *
   * @param {object} entry 流水条目。
   * @return {{text: string, level: string}} 人话与等级。
   */
  function auditSentence(entry) {
    var source = entry || {};
    // 告警先走「人话告警」那条路 ——
    // 实时推来的和快照里重画出来的必须长得一模一样，
    // 不然每 15 秒刷新一次，句子就会变一次。
    if (isAuditAlert(source)) {
      return auditSentenceFromAlert(source);
    }
    var level = String(source.audit_level || AUDIT_LEVEL_OK);
    var preset = String(source.audit_text || "").trim();
    if (preset) {
      return { text: preset, level: level };
    }
    // ⚠️ v3.6.0：流水里的原文也要**挖掉噪声段** ——
    //   后端给的是事件正文的原始前若干字，
    //   而 codex 的用户消息里，真实问题跟 <environment_context>
    //   是挤在同一条里的 —— 不挖的话，这个「核心上下文」气泡
    //   里塞的全是框架的内部提示，用户还是看不到自己问了什么。
    var snippet = clipText(
      stripNoiseSections(source.snippet || ""),
      AUDIT_SNIPPET_LIMIT
    );
    var type = String(source.event_type || "");
    var summary = String(source.summary || "").trim();
    // v3.6.0：用户提问是「核心上下文」——
    // 它跟工具调用不是一回事，必须**单独认出来**，
    // 否则流水区里根本看不见用户到底问了什么。
    if (type === "user_message") {
      return {
        text: "🧑 用户提问：" + (snippet || "…"),
        level: level
      };
    }
    if (type === "thinking") {
      return {
        text: "Agent 正在思考：" + (snippet || "…"),
        level: level
      };
    }
    if (type === "tool_call") {
      var verb = auditVerb(summary);
      if (!verb || verb === "处理") {
        verb = "执行";
      }
      var label = "正常" + verb;
      if (verb === "执行") {
        label = "正常执行命令";
      }
      return { text: label + "：" + (snippet || "…"), level: level };
    }
    if (type === "text") {
      // v3.6.0：与日志面板口径统一 —— 「🤖 Agent 回答：」。
      return { text: "🤖 Agent 回答：" + (snippet || "…"), level: level };
    }
    if (type === "result") {
      return { text: "本轮任务完成。", level: level };
    }
    if (summary) {
      return { text: summary, level: level };
    }
    return { text: "正常处理", level: level };
  }

  /**
   * 这一条流水是不是「核心上下文」（v3.6.0）。
   *
   * ⚠️ 单独一个函数，**不**往 :func:`auditSentence` 的返回值里塞字段 ——
   *   那个返回值被 v3.5.1 的前端夹具逐字比对过，
   *   多一个字段整套断言就全红了。
   *
   * @param {object} entry 流水条目。
   * @return {string} ``user`` / ``reply`` / 空串。
   */
  function auditEntryKind(entry) {
    var source = entry || {};
    if (isAuditAlert(source)) {
      return "";
    }
    var type = String(source.event_type || "");
    if (type === "user_message") {
      return "user";
    }
    if (type === "text") {
      return "reply";
    }
    return "";
  }

  /* ------------------------------------- 审核流水的压缩档位（v3.7.0）--
   *
   * ⚠️ 分段边界必须与设置页 ``settings_pipeline.js`` 的
   *    ``AUDIT_SCALE_BANDS`` **一字不差**：
   *      0  - 39  完整：所有条目（含「Agent 正在思考」）
   *      40 - 69  精简：省略思考过程
   *      70 - 89  结论：只留核心上下文、告警与本轮结论
   *      90 - 100 极限：在「结论」之上，连续的普通流水合并成计数行
   */

  /**
   * 把审核流水档位翻译成模式名（v3.7.0）。
   *
   * @param {number} scale 档位（0-100）。
   * @return {string} ``full`` / ``compact`` / ``conclusion`` / ``extreme``。
   */
  function auditCompressionMode(scale) {
    var value = clampCompression(scale, AUDIT_SCALE_DEFAULT);
    if (value >= AUDIT_SCALE_EXTREME_MIN) {
      return "extreme";
    }
    if (value >= AUDIT_SCALE_CONCLUSION_MIN) {
      return "conclusion";
    }
    if (value > AUDIT_SCALE_THINKING_MAX) {
      return "compact";
    }
    return "full";
  }

  /**
   * 这一条流水是不是「普通流水」（v3.7.0）。
   *
   * 「普通」= **不是告警**的**工具调用** ——
   * 也就是「正常读取：xxx」「正常执行命令：xxx」那一类。
   * 只有它们会被极限档合并成计数行 ——
   * 告警、用户提问、Agent 回答都必须**逐条**露出来。
   *
   * @param {object} entry 流水条目。
   * @return {boolean} 是普通流水返回 ``true``。
   */
  function auditEntryIsPlain(entry) {
    var source = entry || {};
    if (isAuditAlert(source)) {
      return false;
    }
    return String(source.event_type || "") === "tool_call";
  }

  /**
   * 这一条流水是不是「系统提示」（v3.8.0）。
   *
   * 「系统提示」= 带上游写好的 ``audit_text`` 的条目 ——
   * 比如 L2 掉线热切 L3 时那条
   * ``[WARN] L2 掉线，已自动切换至 L3 深度审查``。
   *
   * ⚠️ 这类条目**必须在任何档位都逐条显示** ——
   *    「结论」「极限」档本来只留核心上下文与告警，
   *    要是把系统提示也藏了，用户就完全不知道
   *    「刚才那一轮其实走了云端」——
   *    而这恰恰是最需要知道的事。
   *
   * ⚠️ 告警不算在这里：告警有自己的通道，本来就条条可见。
   *
   * @param {object} entry 流水条目。
   * @return {boolean} 是系统提示返回 ``true``。
   */
  function auditEntryIsNotice(entry) {
    var source = entry || {};
    if (isAuditAlert(source)) {
      return false;
    }
    return String(source.audit_text || "").trim() !== "";
  }

  /**
   * 这一条流水在当前档位下要不要**单独**显示（v3.7.0）。
   *
   * ⚠️ 极限档的普通流水**不**单独显示 ——
   *    它们被合并成计数行（见 :func:`auditCountText`）。
   *
   * @param {object} entry 流水条目。
   * @param {string} mode 档位模式；不传就现读状态。
   * @return {boolean} 要显示返回 ``true``。
   */
  function auditEntryVisible(entry, mode) {
    var current = mode || auditCompressionMode(state.auditCompression);
    var source = entry || {};
    var type = String(source.event_type || "");
    /* v3.8.0：系统提示（``audit_text``）在任何档位都逐条显示。 */
    if (auditEntryIsNotice(source)) {
      return true;
    }
    if (current === "full") {
      return true;
    }
    if (current === "compact") {
      /* 精简：只藏思考过程，其余照旧。 */
      return type !== "thinking";
    }
    if (current === "conclusion") {
      /* 结论：只留核心上下文、告警与本轮结论。 */
      if (isAuditAlert(source)) {
        return true;
      }
      return type === "user_message" || type === "text" || type === "result";
    }
    /* 极限：结论之上再把普通流水合并 —— 它们仍会出现（计数行），
     * 但**不**逐条显示。 */
    if (isAuditAlert(source)) {
      return true;
    }
    return type === "user_message" || type === "text" || type === "result";
  }

  /**
   * 取出这一条流水的时间戳文本（v3.7.0）。
   *
   * ⚠️ 快照里的条目自带 ``time``（后端格式化，时区只有一个解释者）；
   *    SSE 实时推来的没有 —— 那就现场按时间戳算，两条路结果一致。
   *
   * @param {object} entry 流水条目。
   * @return {string} 时间文本；取不到返回空串。
   */
  function auditEntryClock(entry) {
    var source = entry || {};
    return String(source.time || auditClock(source.timestamp) || "");
  }

  /**
   * 把一串普通流水拼成一句计数摘要（v3.7.0）。
   *
   * ⚠️ 刻意**不列**每一条的内容 ——
   *    极限档的意义就是「近几个事情用一句话说完」。
   *
   * @param {object} run 这一串的统计信息。
   * @return {string} 计数摘要。
   */
  function auditCountText(run) {
    var source = run || {};
    var count = Number(source.count || 0);
    var last = String(source.last || "");
    var range = last ? "（至 " + last + "）" : "";
    return "⚡ 极限摘要：正常流水 " + count + " 条" + range;
  }

  /**
   * 把一条告警翻译成流水区的一句人话（v3.5.1）。
   *
   * @param {object} alert 告警字典。
   * @return {{text: string, level: string}} 人话与等级。
   */
  function auditSentenceFromAlert(alert) {
    var source = alert || {};
    var verdict = String(source.verdict || "");
    var severity = String(source.severity || "");
    var level = String(source.level || "");
    var danger =
      verdict === "DANGEROUS" ||
      severity === "red_line" ||
      level === "critical";
    var reason = String(source.reason || "").trim();
    if (danger) {
      var target = String(source.command || "").trim();
      if (!target) {
        // 「匹配到高危命令: xxx」的前缀跟句式重复了，去掉。
        target = reason.replace(/^匹配到高危命令[:：]\s*/, "").trim();
      }
      if (!target) {
        target = String(source.tool || "").trim() || verdict;
      }
      return {
        text: "危险！拦截到高危命令：" + clipText(target, AUDIT_SNIPPET_LIMIT),
        level: AUDIT_LEVEL_DANGER
      };
    }
    return {
      text: "警告：" + clipText(reason || verdict, AUDIT_SNIPPET_LIMIT),
      level: AUDIT_LEVEL_WARN
    };
  }

  /**
   * 把 epoch 秒格式化成流水区的 ``HH:MM:SS``（v3.5.1）。
   *
   * @param {number} stamp epoch 秒。
   * @return {string} 时间文本；非法值返回空串。
   */
  function auditClock(stamp) {
    var value = Number(stamp);
    if (!isFinite(value) || value <= 0) {
      return "";
    }
    var date = new Date(value * 1000);
    return (
      ("0" + date.getHours()).slice(-2) + ":" +
      ("0" + date.getMinutes()).slice(-2) + ":" +
      ("0" + date.getSeconds()).slice(-2)
    );
  }

  /**
   * 把一条告警**也**写进流水区（v3.5.1）。
   *
   * 流水区以前只有「一切正常」的灰字 ——
   * 真正出事的那一刻反而**看不见**。
   * 现在告警也会在流水区留下一句橘黄 / 红色的人话。
   *
   * @param {object} alert 告警字典。
   */
  function appendAuditAlert(alert) {
    if (!alert) {
      return;
    }
    // v3.5.1：告警**本身**就是一条流水条目 ——
    // 服务端快照里的告警流水也是这个形状（stream_kind=alert），
    // 所以「实时追加的那一行」和「刷新后重画出来的那一行」
    // 长得一模一样（同一句话、同一个颜色、同一时刻）。
    appendLiveAudit(alert);
  }

  /* --------------------------------------------- 雷达空态引导（v3.5.1） */
  //
  // 用户实测：「雷达空荡荡的，我不知道该去怎么启动日志。」
  //
  // 光写「未发现」等于把用户晾在原地 ——
  // 必须**直接把可执行的那一条命令**摆出来，再给一键复制。

  /**
   * 拼出「怎么开始监控」的那条命令（v3.6.0）。
   *
   * ⚠️ v3.6.0 的关键改动：**文件还没生成时，只给相对路径**。
   *   v3.5.1 把一个绝对路径摆出来，可那个文件当时根本不存在 ——
   *   用户照抄命令、点【接入】，换回来的是一句冷冰冰的 400。
   *   现在：
   *     文件已存在 → 用**已验证存在**的绝对路径；
   *     文件不存在 → 用相对路径 ``logs/capture.jsonl``，
   *                  并明说「先生成再接入」。
   *
   * @param {boolean} [exists] 捕获文件是否已存在；省略时取当前状态。
   * @param {string} [path] 捕获文件路径；省略时取当前状态。
   * @return {string} 完整命令。
   */
  function radarEmptyCommand(exists, path) {
    var ready =
      exists === undefined ? !!state.radarCaptureExists : !!exists;
    var target =
      path === undefined
        ? String(state.radarCapturePath || "").trim()
        : String(path || "").trim();
    if (!ready || !target) {
      return 'mcode exec "你的指令" > ' + RADAR_EMPTY_RELATIVE_PATH;
    }
    return 'mcode exec "你的指令" > ' + target;
  }

  /**
   * 把命令复制到剪贴板（v3.5.1）。
   *
   * @param {HTMLElement} button 触发按钮（用来回报成功 / 失败）。
   */
  function copyRadarCommand(button) {
    var command = radarEmptyCommand();
    /**
     * 回报结果。
     *
     * @param {boolean} ok 是否复制成功。
     */
    var done = function (ok) {
      if (!button) {
        return;
      }
      button.textContent = ok ? "已复制 ✓" : "复制失败，请手动选中";
      window.setTimeout(function () {
        button.textContent = "复制命令";
      }, 1800);
    };
    /**
     * 老办法：临时 textarea + ``execCommand("copy")``。
     *
     * 剪贴板 API 在**页面失焦**或**没授权**时会被直接拒掉，
     * 这时候还有一条老路可走 —— 总比让用户手动选中强。
     *
     * @return {void}
     */
    var fallback = function () {
      try {
        var area = document.createElement("textarea");
        area.value = command;
        area.setAttribute("readonly", "readonly");
        area.style.position = "fixed";
        area.style.top = "-1000px";
        document.body.appendChild(area);
        area.select();
        var ok = document.execCommand("copy");
        document.body.removeChild(area);
        done(!!ok);
      } catch (err) {
        console.error("复制命令失败：", err);
        done(false);
      }
    };
    var clipboard = navigator.clipboard;
    if (clipboard && typeof clipboard.writeText === "function") {
      clipboard.writeText(command).then(
        function () {
          done(true);
        },
        function (err) {
          // ⚠️ 被拒不等于没戏 —— 先试老办法，再认输。
          console.warn("剪贴板 API 不可用，改用 execCommand：", err);
          fallback();
        }
      );
      return;
    }
    // 老浏览器没有 clipboard API —— 直接走老办法。
    fallback();
  }

  /**
   * 造一个「没发现活跃 Agent」的引导块（v3.5.1）。
   *
   * @return {HTMLElement} 引导块。
   */
  function buildRadarEmptyGuide() {
    var box = document.createElement("div");
    box.className = "radar-empty";

    var title = document.createElement("p");
    title.className = "radar-empty__title";
    title.textContent = RADAR_EMPTY_TITLE;
    box.appendChild(title);

    var command = radarEmptyCommand();

    var code = document.createElement("code");
    code.className = "radar-empty__command mono";
    code.textContent = command;
    box.appendChild(code);

    var actions = document.createElement("div");
    actions.className = "radar-empty__actions";

    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "btn btn--sm btn--primary";
    copy.textContent = "复制命令";
    copy.addEventListener("click", function () {
      copyRadarCommand(copy);
    });
    actions.appendChild(copy);

    var note = document.createElement("span");
    note.className = "radar-empty__note";
    // v3.6.0：文件还没生成时，**必须**告诉用户先生成再接入 ——
    // 否则他点下去只会收到一句看不懂的 400。
    note.textContent = state.radarCaptureExists
      ? RADAR_EMPTY_NOTE_READY
      : RADAR_EMPTY_NOTE_MISSING;
    actions.appendChild(note);

    box.appendChild(actions);
    return box;
  }

  /**
   * 刷新左侧的「没发现活跃 Agent」引导块（v3.5.1）。
   *
   * 只有「一个活跃 Agent 都没有、而且也没有挂着任何会话」时才出现 ——
   * 已经在监控了还弹这个，等于噪声。
   *
   * @param {number} activeCount 雷达扫到的活跃 Agent 进程数。
   */
  function updateRadarEmpty(activeCount) {
    var box = el.radarEmpty;
    if (!box) {
      return;
    }
    var sessions = (state.sessions || []).filter(function (item) {
      return !item.placeholder && String(item.id) !== "radar";
    });
    var needed =
      Number(activeCount || 0) === 0 && sessions.length === 0;
    if (!needed) {
      box.hidden = true;
      box.textContent = "";
      box.removeAttribute("data-command");
      return;
    }
    var command = radarEmptyCommand();
    // v3.6.0：缓存键必须**带上存在性** ——
    //   文件从「没有」变成「有」时，命令与提示语都得换，
    //   只比命令的话提示语会停在旧的那句上。
    var ready = state.radarCaptureExists ? "1" : "0";
    if (
      box.getAttribute("data-command") === command &&
      box.getAttribute("data-ready") === ready &&
      box.firstElementChild
    ) {
      box.hidden = false;
      return;
    }
    box.textContent = "";
    box.appendChild(buildRadarEmptyGuide());
    box.setAttribute("data-command", command);
    box.setAttribute("data-ready", ready);
    box.hidden = false;
  }

  /* --------------------------------------------------------- 工具 */

  function nowText() {
    var d = new Date();
    return (
      String(d.getHours()).padStart(2, "0") +
      ":" +
      String(d.getMinutes()).padStart(2, "0") +
      ":" +
      String(d.getSeconds()).padStart(2, "0")
    );
  }

  function alertTime(alert) {
    var ts = Number(alert && alert.timestamp);
    if (!ts) {
      return "--:--:--";
    }
    var d = new Date(ts * 1000);
    return (
      String(d.getHours()).padStart(2, "0") +
      ":" +
      String(d.getMinutes()).padStart(2, "0") +
      ":" +
      String(d.getSeconds()).padStart(2, "0")
    );
  }

  function severityClass(verdict) {
    var name = String(verdict || "").toUpperCase();
    if (name === "DANGEROUS") {
      return "danger";
    }
    if (name === "DRIFT") {
      return "drift";
    }
    if (name === "LOOP") {
      return "warn";
    }
    return "plain";
  }

  function isRedLine(alert) {
    return String((alert && alert.severity) || "") === "red_line";
  }

  /* --------------------------------------------------------- 守护者 */

  function guardianArmed() {
    var info = state.guardian || {};
    return !!(info.available && info.enabled);
  }

  function updateGuardianChip() {
    if (el.guardianChip) {
      var armed = guardianArmed();
      el.guardianChip.dataset.armed = armed ? "true" : "false";
      el.guardianChip.textContent = armed
        ? "GUARDIAN 武装"
        : "GUARDIAN 关闭";
      el.guardianChip.title = armed
        ? "守护者已启用：红线级事件会真的挂起被监控进程（可随时放行）"
        : "守护者未启用：只告警，不干预被监控进程";
    }
    refreshGuardianButtons();
  }

  /* 按钮的可用状态不能在渲染那一刻就定死 ——
   * 告警卡片往往比守护者状态先到。
   * 状态一变化就回头把已有卡片的按钮重新解锁。 */
  function refreshGuardianButtons() {
    var armed = guardianArmed();
    // ⚠️ 只管**干预**按钮。
    //
    // 【复制纠偏提示】/【发送给 Agent】/【一键回滚】都是
    // **只读便利功能** —— 复制一段文字、读一份快照，
    // 跟守护者有没有武装毫无关系。
    // 早先这里用 `.alert-actions .btn` 一网打尽，
    // 结果守护者没武装（比如演示模式、或还没拉到 PID）时，
    // 用户连「复制一段提示」都点不了 —— 那就白做了这个功能。
    // （``disabled`` 的按钮连 click 事件都不会触发。）
    var buttons = document.querySelectorAll(
      ".alert-actions .btn:not(.alert-card__aux)"
    );
    Array.prototype.forEach.call(buttons, function (button) {
      button.disabled = !armed;
    });
    var hints = document.querySelectorAll(".alert-actions__hint");
    Array.prototype.forEach.call(hints, function (hint) {
      hint.textContent = armed
        ? "守护者已武装 · 可逆干预"
        : "守护者未启用 · 按钮不可用";
    });
  }

  function loadGuardianStatus() {
    return fetch("/api/guardian/status")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        state.guardian = data || { available: false, enabled: false };
        updateGuardianChip();
      })
      .catch(function (err) {
        state.guardian = { available: false, enabled: false };
        updateGuardianChip();
        pushConsole("读取守护者状态失败：" + err.message, "line");
      });
  }

  /* ------------------------------------------------------ 任务目标 */

  function updateGoalUi(goal) {
    state.taskGoal = goal || "";
    if (el.goalInput && el.goalInput.value !== state.taskGoal) {
      el.goalInput.value = state.taskGoal;
    }
    if (el.goalForm) {
      el.goalForm.dataset.active = state.taskGoal ? "true" : "false";
    }
  }

  function loadTaskGoal() {
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) + "/task_goal"
      : "/api/task_goal";
    return fetch(url)
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        updateGoalUi((data || {}).goal || "");
      })
      .catch(function (err) {
        pushConsole("读取任务目标失败：" + err.message, "line");
      });
  }

  /* ------------------------------------------------ 当前监控目标面板 */

  /**
   * 把一条路径缩成「最后两段」（列表里显示用）。
   *
   * @param {string} path 路径。
   * @return {string} 形如「work\project_alpha」。
   */
  function shortPath(path) {
    var text = String(path || "").replace(/[\\/]+$/, "");
    if (!text) {
      return "";
    }
    var parts = text.split(/[\\/]/).filter(function (item) {
      return item.length > 0;
    });
    if (parts.length <= 2) {
      return parts.join("\\");
    }
    return parts.slice(-2).join("\\");
  }

  function updateTargetUi(data) {
    var info = data || {};
    state.watchTarget = info;
    if (el.watchTarget) {
      el.watchTarget.dataset.source = String(info.goal_source || "none");
    }
    if (el.watchAgent) {
      // v2.3.0：目标面板要**醒目**地显示「[Codex] D:\work\project_alpha」。
      var agent = String(info.agent || "");
      var workspace = String(info.workspace || "");
      var label = agent
        ? "[" + agentLabel(agent) + "] " + (workspace || "（未提供工作区）")
        : "–";
      el.watchAgent.textContent = label;
      el.watchAgent.hidden = !agent && !workspace;
      el.watchAgent.title = workspace || agent;
    }
    if (el.watchGoalSource) {
      el.watchGoalSource.textContent = String(
        info.goal_source_label || info.goal_source || "未设置"
      );
      // v3.3.1：**没有目标**时，「未设置」不能只是灰字 ——
      // 它得跳出来（橘黄高亮）+ 旁边挂一个气泡，
      // 用户才知道该往顶部输入框里填什么。
      var missingGoal = String(info.goal_source || "none") === "none";
      el.watchGoalSource.classList.toggle(
        "watch-target__source--none",
        missingGoal
      );
      el.watchGoalSource.setAttribute("role", "button");
      el.watchGoalSource.setAttribute("tabindex", missingGoal ? "0" : "-1");
      el.watchGoalSource.title = missingGoal ? GOAL_NUDGE_TEXT : "";
    }
    if (el.watchGoalNudge) {
      // 气泡**只在没有目标时**出现 ——
      // 目标一旦设好，它就该闭嘴，把位置还给正常状态。
      el.watchGoalNudge.hidden =
        String((info.goal_source || "none")) !== "none";
    }
    if (el.watchGoal) {
      var goal = String(info.goal || "");
      el.watchGoal.textContent = goal || "（尚未锚定任务目标）";
      el.watchGoal.classList.toggle("watch-target__goal--empty", !goal);
    }
  }

  /**
   * 把光标送到顶部的任务目标输入框（v3.3.1 强引导）。
   *
   * 「未设置」高亮与旁边的气泡都调它 ——
   * 用户的下一步只有一个动作：**把目标填上**。
   */
  function focusGoalInput() {
    if (!el.goalInput) {
      return;
    }
    el.goalInput.focus();
    if (typeof el.goalInput.select === "function") {
      el.goalInput.select();
    }
  }

  function loadWatchTarget() {
    return fetch("/api/watch_target")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        updateTargetUi(data || {});
      })
      .catch(function (err) {
        pushConsole("读取监控目标失败：" + err.message, "line");
      });
  }

  /* ---------------------------------------------------- 多路会话中枢 */

  function sessionStateLabel(name) {
    var map = {
      starting: "启动中",
      running: "运行中",
      paused: "已挂起",
      idle: "空闲",
      finished: "已结束",
      // v3.4.0：进程死了但日志文件还在 ——
      // 「运行中」是假的，这个状态说清楚「它已经静默了」。
      exited: "已退出（静默）"
    };
    return map[String(name || "")] || String(name || "未知");
  }

  function isActiveSession(sessionId) {
    return !state.activeSession || String(sessionId) === state.activeSession;
  }

  function findSession(sessionId) {
    var list = state.sessions || [];
    for (var i = 0; i < list.length; i += 1) {
      if (String(list[i].id) === String(sessionId)) {
        return list[i];
      }
    }
    return null;
  }

  /* ------------------------------------------- 虚拟会话（v3.5.0）
   *
   * 一个日志文件里混着多个 Agent 时，告警流其实是**交织**的。
   * 后端按**线程指纹**把它们分成多路（防串台），
   * 前端据此：
   *
   *   1. 左侧把同一份日志拆成多张**虚拟会话卡片**；
   *   2. 选中一张时，右侧的告警与流水**只显示那一路**。
   */

  /**
   * 取一条记录所属的「流标识」。
   *
   * 认不出的事件（没有指纹）归到 ``default`` ——
   * 与后端 :func:`fingerprint_stream_key` 的口径**完全一致**。
   *
   * @param {object} item 告警 / 流水条目。
   * @return {string} 流标识。
   */
  function streamKeyOf(item) {
    var fingerprint = String((item && item.fingerprint) || "");
    return fingerprint || "default";
  }

  /**
   * 当前是不是只显示某一路。
   *
   * @param {object} item 告警 / 流水条目。
   * @return {boolean} 该属于当前显示范围返回 ``true``。
   */
  function matchesStream(item) {
    var filter = state.streamFilter || {};
    if (!filter.key) {
      return true;
    }
    return streamKeyOf(item) === filter.key;
  }

  /**
   * 切换「只看某一路虚拟会话」。
   *
   * @param {string} sessionId 所属会话 ID。
   * @param {string} key        流标识。
   */
  function setStreamFilter(sessionId, key) {
    state.streamFilter = {
      sessionId: String(sessionId || ""),
      key: String(key || "")
    };
    refreshStreamFilter();
  }

  /**
   * 取消筛选 —— 恢复显示全部。
   */
  function clearStreamFilter() {
    state.streamFilter = { sessionId: "", key: "" };
    refreshStreamFilter();
  }

  /**
   * 筛选状态变了以后，把界面刷一遍。
   *
   * 告警列表的「签名」里带着筛选条件 ——
   * 不然改了筛选，旧签名没变，界面**不会**重绘。
   */
  function refreshStreamFilter() {
    updateStreamFilterUi();
    renderSessionList();
    state.renderedAlertIds = "";
    renderAlerts(state.alerts || []);
    renderLiveAudit(state.liveAuditAll || []);
  }

  /**
   * 刷新「只看这一路」的提示按钮。
   */
  function updateStreamFilterUi() {
    if (!el.streamFilter) {
      return;
    }
    var filter = state.streamFilter || {};
    if (!filter.key) {
      el.streamFilter.hidden = true;
      return;
    }
    el.streamFilter.hidden = false;
    if (el.streamFilterText) {
      el.streamFilterText.textContent = "只看：" + streamLabel(filter);
    }
  }

  /**
   * 把流标识翻译成一句人能看懂的标签。
   *
   * @param {object} filter 当前筛选条件。
   * @return {string} 标签文本。
   */
  function streamLabel(filter) {
    var key = String((filter && filter.key) || "");
    var session = findSession(filter && filter.sessionId);
    var streams = (session && session.streams) || [];
    for (var i = 0; i < streams.length; i += 1) {
      if (String(streams[i].key) === key) {
        return String(streams[i].label || key);
      }
    }
    return key || "默认流";
  }

  /**
   * 在会话卡片下面渲染**虚拟会话卡片**（按线程指纹拆）。
   *
   * 只有一路的时候**不渲染** —— 一张卡下面再挂一个「默认流」
   * 只会徒增噪音。
   *
   * @param {object} session 会话快照。
   * @param {HTMLElement} item 会话卡片的 DOM 节点。
   */
  function renderStreamCards(session, item) {
    var streams = session.streams || [];
    if (streams.length < 2) {
      return;
    }
    var box = document.createElement("div");
    box.className = "session-streams";
    var filter = state.streamFilter || {};
    streams.forEach(function (stream) {
      var key = String(stream.key || "default");
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "session-stream";
      chip.dataset.streamKey = key;
      chip.title = "只看这一路（" + key + "）";
      if (
        String(session.id) === String(filter.sessionId) &&
        key === String(filter.key)
      ) {
        chip.classList.add("session-stream--active");
        chip.setAttribute("aria-current", "true");
      }
      var name = document.createElement("span");
      name.className = "session-stream__name";
      name.textContent = String(stream.label || key);
      chip.appendChild(name);
      var count = document.createElement("span");
      count.className = "session-stream__count";
      count.textContent =
        String(stream.alerts || 0) + " 告警 / " +
        String(stream.ontrack || 0) + " 正常";
      chip.appendChild(count);
      chip.addEventListener("click", function (event) {
        // ⚠️ 卡片本身有点击切换会话的监听 ——
        //    不拦住的话，点虚拟卡片会连带切会话。
        event.stopPropagation();
        var current = state.streamFilter || {};
        if (
          String(session.id) === String(current.sessionId) &&
          String(current.key) === key
        ) {
          clearStreamFilter();
        } else {
          setStreamFilter(String(session.id), key);
        }
      });
      box.appendChild(chip);
    });
    item.appendChild(box);
  }

  /* 左侧会话列表 —— 每一路 Agent 一个条目，
   * 点一下就能切过去看它的事件流与告警。 */

  /**
   * 会话列表的「签名」—— 只有签名变了才重建 DOM。
   *
   * v4.1.0：吸收外部插件面板的「列表没变就不动它」。
   *
   * 为什么不能「清空重建」
   * ====================
   *
   * 看板每 5 秒拉一次会话列表。早先的实现是「清空 → 重建所有条目」，
   * 于是出现了两个很讨厌的现象：
   *
   * * 用户正在滚动列表 → 滚动位置被重置；
   * * 用户正要点的那个条目被**抽走重建** → 点了像没反应。
   *
   * 现在改成：签名没变就**一个 DOM 都不动**。
   *
   * ⚠️ 签名里必须带上**流筛选条件** ——
   * 流筛选会改变条目里那些「默认流 / 线程 X」小标签的高亮状态，
   * 不带的话改了筛选界面不会重绘。
   *
   * @param {Array} list 会话快照列表。
   * @return {string} 签名串。
   */
  function sessionListSignature(list) {
    var filter = state.streamFilter || {};
    var head = [
      String(filter.sessionId || ""),
      String(filter.key || "")
    ].join("\u0003");
    return head + "\u0002" + (list || []).map(function (session) {
      return [
        String(session.id),
        String(session.state || ""),
        String(session.workspace || ""),
        String(session.agent || ""),
        String(session.alert_total || 0),
        String(session.pid || 0),
        session.process_bound ? "1" : "0",
        session.attached ? "1" : "0",
        session.placeholder ? "1" : "0",
        String(session.goal || ""),
        String(session.goal_source || ""),
        String(session.label || ""),
        (session.streams || []).map(function (stream) {
          return String(stream.key || "") + ":" +
            String(stream.alerts || 0) + ":" +
            String(stream.ontrack || 0);
        }).join("/")
      ].join("\u0001");
    }).join("\u0002");
  }

  function renderSessionList() {
    var list = state.sessions || [];
    if (el.sessionsHint) {
      el.sessionsHint.textContent = list.length + " 路";
    }
    updateGoalHint();
    if (!el.sessionList) {
      return;
    }
    // v4.1.0：签名没变就**不动 DOM** ——
    // 别把用户正在看 / 正要点的条目抽走重建。
    var signature = sessionListSignature(list);
    if (signature === state.renderedSessionSig) {
      return;
    }
    state.renderedSessionSig = signature;
    while (el.sessionList.firstElementChild) {
      el.sessionList.removeChild(el.sessionList.firstElementChild);
    }
    if (!list.length) {
      var empty = document.createElement("li");
      empty.className = "sessions__empty";
      empty.textContent = "暂无会话";
      el.sessionList.appendChild(empty);
      return;
    }
    list.forEach(function (session) {
      var item = document.createElement("li");
      item.className = "session-item";
      item.dataset.sessionId = String(session.id);
      item.dataset.state = String(session.state || "idle");
      if (String(session.id) === state.activeSession) {
        item.classList.add("session-item--active");
        item.setAttribute("aria-current", "true");
      }

      var name = document.createElement("span");
      name.className = "session-item__name";
      // v2.4.0：挂载进来的会话要**如实**标出进程绑定情况 ——
      // 反查到了就写 PID，查不到就写「[未绑定进程]」，
      // 绝不含糊过去（用户要靠它判断能不能武装守护者）。
      name.textContent = sessionLabel(session);
      name.title = sessionFullLabel(session);
      item.appendChild(name);

      var agent = document.createElement("span");
      agent.className = "session-item__agent";
      // v2.3.0：有工作区就显示工作区（那才是「跑的是哪个项目」），
      // 没有才退回显示 Agent 类型。
      var workspace = String(session.workspace || "");
      if (workspace) {
        agent.textContent = shortPath(workspace);
        agent.title = workspace;
      } else {
        agent.textContent = agentLabel(session.agent) || "—";
      }
      item.appendChild(agent);

      var badge = document.createElement("span");
      badge.className = "session-item__state";
      badge.dataset.state = String(session.state || "idle");
      badge.textContent = sessionStateLabel(session.state);
      item.appendChild(badge);

      var count = document.createElement("span");
      count.className = "session-item__count";
      count.textContent = String(session.alert_total || 0);
      count.title = "告警数";
      item.appendChild(count);

      // v3.4.0：卸载按钮（``[×]``）——
      //
      // 用户实测：「UI 无法删除选错的/失效的挂载会话。」
      // 挂错了只能干看着 —— 现在一键就能卸掉。
      //
      // ⚠️ **雷达自己那个占位会话不给这个按钮** ——
      //    它不是「挂载进来的会话」，卸了没有意义，
      //    用户看到的却是「为什么这个也能删」。
      if (!session.placeholder && session.id !== "radar") {
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "session-item__remove";
        remove.textContent = "×";
        remove.title = "卸载这个会话（停止跟随它的日志）";
        remove.setAttribute("aria-label", "卸载会话 " + String(session.label || session.id));
        remove.addEventListener("click", function (event) {
          // ⚠️ 卡片本身有点击切换会话的监听 ——
          //    不拦住的话，点【×】会连带切到那个会话。
          event.stopPropagation();
          detachSession(String(session.id));
        });
        item.appendChild(remove);
      }

      // v2.4.0：【武装守护者】开关 ——
      //
      // 只有**挂载**进来的会话才有这个开关；而且**只有用户手动打开**
      // 之后，守护者才会真的去接管那个 PID。
      // 反查不到进程时开关是**禁用**的，并说清楚为什么。
      if (session.attached) {
        item.classList.add("session-item--attach");
        var pid = Number(session.pid || 0);
        var pidTag = document.createElement("span");
        pidTag.className = "session-item__pid";
        pidTag.dataset.bound = session.process_bound && pid ? "true" : "false";
        pidTag.appendChild(
          document.createTextNode(
            session.process_bound && pid ? "PID " + pid : "[未绑定进程]"
          )
        );
        // v3.0.0：目标是从日志里**自动提取**出来的 ——
        // 标出来，用户才知道「原来不用自己填」。
        if (session.goal_source === "auto") {
          var autoTag = document.createElement("span");
          autoTag.className = "session-item__auto";
          autoTag.textContent = "[自动提取]";
          autoTag.title =
            "任务目标是从这份日志的第一条用户消息里自动提取的。";
          pidTag.appendChild(autoTag);
        }
        pidTag.title = session.process_bound && pid
          ? "反查到的进程 PID：" + pid
          : UNBOUND_PID_HINT + " " + UNBOUND_PID_STILL_WORKS;
        item.appendChild(pidTag);
        item.appendChild(buildArmButton(session));
        var armHint = buildArmHint(session);
        if (armHint) {
          item.appendChild(armHint);
        }
      }

      item.addEventListener("click", function () {
        switchSession(String(session.id));
      });
      // v3.5.0：同一份日志里混着多个 Agent 时，
      // 在卡片下面挂上按**线程指纹**拆出来的虚拟会话卡片。
      renderStreamCards(session, item);
      el.sessionList.appendChild(item);
    });
  }

  /**
   * 会话在列表里的展示名。
   *
   * 挂载会话要带上进程绑定情况（v2.4.0）：
   * 反查到了写「(PID: 12345)」，查不到写「[未绑定进程]」。
   *
   * @param {object} session 会话快照。
   * @return {string} 展示名。
   */
  function sessionLabel(session) {
    var text = String((session && session.label) || (session && session.id) || "");
    if (!session || !session.attached) {
      return text;
    }
    var pid = Number(session.pid || 0);
    if (session.process_bound && pid) {
      return text + " (PID: " + pid + ")";
    }
    return text + " [未绑定进程]";
  }

  /**
   * 会话的完整标签（放进悬浮提示）。
   *
   * 侧边栏窄，长路径会被省略号截断 ——
   * 完整信息必须**看得到**（悬浮提示里）。
   *
   * @param {object} session 会话快照。
   * @return {string} 完整标签。
   */
  function sessionFullLabel(session) {
    if (!session || !session.attached) {
      return String((session && session.label) || "");
    }
    var pid = Number(session.pid || 0);
    if (session.process_bound && pid) {
      return String(session.label || "") + " (PID: " + pid + ")";
    }
    // v3.3.1：查不到进程**不是故障** ——
    // 把「为什么」和「怎么办」一起说清楚，
    // 免得用户看着灰掉的开关以为整个哨兵都不工作了。
    return (
      String(session.label || "") +
      " [未绑定进程] —— " +
      UNBOUND_PID_HINT +
      " " +
      UNBOUND_PID_STILL_WORKS
    );
  }

  /**
   * 挂载会话的提示语（v2.4.0 起，v3.0.0 改写）——
   *
   * v2.4.0 挂载进来的会话**没有**最初的 Prompt，
   * 用户必须手动注入目标才能开意图漂移监控。
   *
   * ⚠️ v3.0.0 起这句话**不能照抄**了 ——
   * 哨兵会自己从日志开头把首条用户消息提取成目标，
   * 这时候再说「没有初始 Prompt」就是**说假话**。
   * 所以提示语要跟着 ``goal_source`` 变。
   */
  function updateGoalHint() {
    if (!el.goalHint) {
      return;
    }
    var session = findSession(state.activeSession);
    if (!session || !session.attached) {
      el.goalHint.hidden = true;
      return;
    }
    el.goalHint.hidden = false;
    var source = String(session.goal_source || "none");
    if (source === "auto") {
      el.goalHint.textContent =
        "目标已从这份日志的首条用户消息自动提取 —— 想改随时可以改。";
    } else if (source === "none") {
      el.goalHint.textContent =
        "挂载会话没有初始 Prompt —— 在此设定目标即开启意图漂移（DRIFT）监控";
    } else {
      el.goalHint.textContent =
        "目标已设定（" + String(session.goal_source_label || source) +
        "）—— 想改随时可以改。";
    }
  }

  /**
   * 造【武装守护者】开关按钮（v2.4.0）。
   *
   * @param {object} session 会话快照。
   * @return {HTMLButtonElement} 按钮元素。
   */
  function buildArmButton(session) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "session-item__arm";
    var armed = !!session.armed;
    var pid = Number(session.pid || 0);
    var confidence = Number(
      (session.process_info && session.process_info.confidence) || 0
    );
    // v3.0.0：高置信度 → **建议**开启（只是建议，绝不自动武装）。
    var suggest = !armed && session.process_bound &&
      confidence >= ARM_SUGGEST_CONFIDENCE;
    button.textContent = armed ? "已武装" : (suggest ? "建议武装" : "武装");
    button.setAttribute("aria-pressed", armed ? "true" : "false");
    button.dataset.armed = armed ? "true" : "false";
    button.dataset.suggest = suggest ? "true" : "false";
    if (!session.process_bound || !pid) {
      // 没绑定到进程 —— 开关**禁用**，并说清楚原因。
      //
      // v3.3.1：只说「无法武装」会让人以为整个哨兵瘫了；
      // 补一句「告警与推送照常」，把话说全。
      button.disabled = true;
      button.title =
        "未绑定到进程，无法挂起（拦截）这个 Agent。" +
        " " + UNBOUND_PID_HINT + " " + UNBOUND_PID_STILL_WORKS;
    } else if (suggest) {
      button.title =
        "高置信度匹配（" + confidence.toFixed(2) +
        "），建议开启守护者 —— 由你决定，哨兵不会自动开。";
    } else {
      button.title = armed
        ? "点击解除武装：哨兵将恢复为只读监控（PID " + pid + "）"
        : "点击武装守护者：红线级告警会真的挂起 PID " + pid;
    }
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      toggleArm(String(session.id), !armed);
    });
    return button;
  }

  /**
   * 造「建议武装」的气泡提示（v3.0.0）。
   *
   * 只在高置信度时出现 —— 鼠标移上去才展开，
   * 不然侧边栏会被这些字塞满。
   *
   * @param {object} session 会话快照。
   * @return {?HTMLElement} 提示元素；不需要时返回 ``null``。
   */
  function buildArmHint(session) {
    var confidence = Number(
      (session.process_info && session.process_info.confidence) || 0
    );
    if (!session.process_bound || session.armed ||
        confidence < ARM_SUGGEST_CONFIDENCE) {
      return null;
    }
    var hint = document.createElement("span");
    hint.className = "session-item__hint";
    hint.textContent =
      "高置信度匹配（" + confidence.toFixed(2) + "），建议开启守护者";
    return hint;
  }

  /**
   * 打开 / 关闭某个会话的【武装守护者】开关（v2.4.0）。
   *
   * @param {string} sessionId 会话 ID。
   * @param {boolean} armed 目标状态。
   */
  function toggleArm(sessionId, armed) {
    fetch("/api/sessions/" + encodeURIComponent(sessionId) + "/arm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ armed: armed })
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          showToast((data && data.error) || "武装失败", true);
          pushConsole(
            "武装失败：" + ((data && data.error) || "未知原因"),
            "alert"
          );
          return;
        }
        showToast(String(data.message || (armed ? "已武装" : "已解除武装")));
        pushConsole(
          "守护者武装 · " + sessionId + " → " +
            (armed ? "已武装" : "已解除"),
          "event"
        );
        loadSessions();
        loadSessionGuardian(sessionId);
      })
      .catch(function (err) {
        showToast("武装失败：" + err.message, true);
      });
  }

  /* 顶部「全局状态」条 —— 所有 Agent 一眼可见：
   * Codex-运行中，mcode-已挂起，Claude-已结束。 */
  function renderGlobalState() {
    if (!el.globalStateChips) {
      return;
    }
    while (el.globalStateChips.firstElementChild) {
      el.globalStateChips.removeChild(el.globalStateChips.firstElementChild);
    }
    var list = state.sessions || [];
    if (!list.length) {
      var empty = document.createElement("span");
      empty.className = "global-state__empty";
      empty.textContent = "暂无会话";
      el.globalStateChips.appendChild(empty);
      return;
    }
    list.forEach(function (session) {
      var chip = document.createElement("span");
      chip.className = "global-state__chip";
      chip.dataset.state = String(session.state || "idle");
      var agent = String(session.agent || "")
        ? agentLabel(session.agent)
        : String(session.label || session.id);
      chip.textContent =
        agent + "-" + sessionStateLabel(session.state);
      chip.title = String(session.goal || "（未锚定任务目标）");
      if (String(session.id) === state.activeSession) {
        chip.classList.add("global-state__chip--active");
      }
      chip.addEventListener("click", function () {
        switchSession(String(session.id));
      });
      el.globalStateChips.appendChild(chip);
    });
  }

  function renderSessions() {
    renderSessionList();
    renderGlobalState();
    // v3.6.0：折叠窄条上要显示「有几路会话」——
    //   收起了也得知道自己在监控几个。
    updateSessionsRailCount();
  }

  function loadSessions() {
    return fetch("/api/sessions")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        var list = (data && data.sessions) || [];
        state.sessions = list;
        var active = state.activeSession;
        var stillThere = list.some(function (session) {
          return String(session.id) === String(active);
        });
        if (!stillThere) {
          // 当前会话没了（结束或被移除）→ 退到服务端说的那一个。
          active = (data && data.active) || (list[0] && list[0].id) || "";
          state.activeSession = String(active || "");
        }
        renderSessions();
        return state.activeSession;
      })
      .catch(function (err) {
        pushConsole("读取会话列表失败：" + err.message, "line");
        return state.activeSession;
      });
  }

  function clearAlerts() {
    if (!el.alertList) {
      return;
    }
    while (el.alertList.firstElementChild) {
      el.alertList.removeChild(el.alertList.firstElementChild);
    }
    state.renderedAlertIds = "";
  }

  /**
   * 渲染告警列表 —— **列表没变就不动它**。
   *
   * 为什么不能「清空重建」
   * ====================
   *
   * 看板每 5 秒拉一次告警列表。早先的实现是「清空 → 重建所有卡片」，
   * 于是出现了一个很讨厌的现象：
   *
   * * 用户正要按下的【复制纠偏提示】按钮被抽走重建了 —— **点了没反应**；
   * * 「解释淡入」的动画被打断；
   * * 焦点和滚动位置一起丢掉。
   *
   * 现在改成：只有告警**集合真的变了**才重建。
   * 平时列表是稳的，卡片也稳，按钮就按得动。
   *
   * @param {Array} alerts 服务端返回的告警列表（**旧 → 新**）。
   */
  function renderAlerts(alerts) {
    if (!el.alertList) {
      return;
    }
    var all = alerts || [];
    // 记住**未筛选**的原始列表 —— 切换虚拟会话时要拿它重画。
    state.alerts = all;
    // v3.5.0：按虚拟会话筛选 —— 只显示当前选中的那一路，
    // 各路互不干扰（防串台）。
    var list = all.filter(matchesStream);
    // 签名里必须带上筛选条件 ——
    // 不然改了筛选，签名没变，界面**不会**重绘。
    var signature =
      String((state.streamFilter || {}).key || "") + "|" +
      list
        .map(function (alert) {
          return String((alert && alert.alert_id) || "");
        })
        .join("|");
    if (signature === state.renderedAlertIds) {
      return;
    }
    clearAlerts();
    state.renderedAlertIds = signature;
    if (!list.length) {
      // 空列表要把「暂无告警」的占位提示放回去 ——
      // 它在模板里是静态节点，被 clearAlerts 一起带走了。
      if (el.alertEmpty && !el.alertEmpty.parentNode) {
        el.alertList.appendChild(el.alertEmpty);
      }
      return;
    }
    list
      .slice()
      .reverse()
      .forEach(function (alert) {
        prependAlert(alert);
      });
  }

  /* ------------------------------------------------ 实时审核流水（v3.3.1） */

  /**
   * 流水区最多保留多少行。
   *
   * 多了会把背景层撑得又高又挤 —— 而它只是**底噪**，
   * 留住最近的一百来行足够看出「它一直在动」。
   */
  var LIVE_AUDIT_LIMIT = 120;

  /**
   * 把流水区的**初始提示**放回去（v3.4.0）。
   *
   * 用户实测：「看不到流式滚动，感官上像个死系统。」
   * 空面板看着像卡住了 —— 所以明说「我在读，只是一切正常」。
   */
  function resetLiveAuditPlaceholder() {
    if (!el.liveAuditLines) {
      return;
    }
    var placeholder = document.getElementById("live-audit-placeholder");
    if (placeholder) {
      return;
    }
    var node = document.createElement("p");
    node.className = "live-audit__placeholder";
    node.id = "live-audit-placeholder";
    node.textContent =
      "正在实时读取日志流，若无告警，此处将滚动显示正常操作。";
    el.liveAuditLines.appendChild(node);
  }

  /**
   * 清空实时审核流水。
   */
  function clearLiveAudit() {
    if (!el.liveAuditLines) {
      return;
    }
    while (el.liveAuditLines.firstElementChild) {
      el.liveAuditLines.removeChild(el.liveAuditLines.firstElementChild);
    }
    state.liveAudit = [];
    state.liveAuditAll = [];
    /* v4.1.0：签名也得清 —— 不然「清空后重放」会被签名判定跳过。 */
    state.renderedLiveAuditSig = "";
    /* v3.7.0：正在累积的普通流水串也要清掉 ——
     * 否则重画以后旧的累积状态会串到新内容上。 */
    state.auditPlainRun = null;
    if (el.liveAuditHint) {
      el.liveAuditHint.textContent = "等待事件…";
    }
    resetLiveAuditPlaceholder();
  }

  /**
   * 追加一行实时审核流水（v3.3.1，v3.7.0 加压缩档位）。
   *
   * 一行就是一次「L1 判定为正常」——
   * 不显示完整 JSON，只显示意图摘要。
   *
   * ⚠️ v3.7.0：这一段现在**受档位控制**（服务端 ``/api/presentation``）——
   *    40 档起省略思考过程，70 档起只留核心上下文与告警，
   *    90 档起连续的普通流水还会被**合并成一条计数行**。
   *
   * @param {object} entry 服务端推来的流水条目。
   */
  function appendLiveAudit(entry) {
    if (!el.liveAuditLines || !entry) {
      return;
    }
    // v3.5.1：流水区说的是**人话**，还带颜色等级 ——
    // 绿 = 正常，橘黄 = 可能有问题，红 = 严重问题。
    var sentence = auditSentence(entry);
    var summary = sentence.text;
    if (!summary) {
      return;
    }
    // 未筛选的列表**照收** —— 切换虚拟会话时要拿它重画。
    state.liveAuditAll.push(entry);
    if (state.liveAuditAll.length > LIVE_AUDIT_LIMIT) {
      state.liveAuditAll = state.liveAuditAll.slice(-LIVE_AUDIT_LIMIT);
    }
    // v3.5.0：按虚拟会话筛选 —— 只显示当前选中的那一路。
    if (!matchesStream(entry)) {
      return;
    }
    // v3.7.0：档位决定这一条要不要**单独**显示。
    var mode = auditCompressionMode(state.auditCompression);
    var plain = mode === "extreme" && auditEntryIsPlain(entry);
    if (!plain && !auditEntryVisible(entry, mode)) {
      // 这一档位下不显示（思考过程 / 普通工具调用）——
      // 但它**已经记过了**，「已记录 N 条」照算。
      countLiveAudit(entry);
      return;
    }
    // v3.4.0：第一条真实流水进来，初始提示就让位。
    var placeholder = document.getElementById("live-audit-placeholder");
    if (placeholder && placeholder.parentNode) {
      placeholder.parentNode.removeChild(placeholder);
    }
    if (plain) {
      // 极限档：普通流水不单独成行 —— 连续的一串合并成计数行。
      appendAuditPlainRun(entry, sentence);
    } else {
      // 这一条打断了连续 —— 上一串普通流水到此封口。
      state.auditPlainRun = null;
      appendAuditLine(entry, sentence);
    }
    countLiveAudit(entry);
    // 始终停在最新一行 —— 流水是**底噪**，不是让人回翻的档案。
    el.liveAuditLines.scrollTop = el.liveAuditLines.scrollHeight;
    if (el.liveAuditHint) {
      el.liveAuditHint.textContent = "已记录 " + state.liveAudit.length + " 条";
    }
  }

  /**
   * 记一条流水进计数（v3.7.0）。
   *
   * ⚠️ 计数是**记录条数**，不是**显示行数** ——
   *    极限档下显示的行会少于记录条数（合并成计数行了），
   *    所以「已记录 N 条」必须按记录数算，不能按渲染行数算。
   *
   * @param {object} entry 流水条目。
   * @return {void}
   */
  function countLiveAudit(entry) {
    state.liveAudit.push(entry);
    if (state.liveAudit.length > LIVE_AUDIT_LIMIT) {
      state.liveAudit = state.liveAudit.slice(-LIVE_AUDIT_LIMIT);
    }
  }

  /**
   * 渲染一行普通流水（v3.7.0）。
   *
   * @param {object} entry 流水条目。
   * @param {object} sentence 人话与等级。
   * @return {HTMLElement} 行元素。
   */
  function appendAuditLine(entry, sentence) {
    var line = document.createElement("div");
    line.className =
      "live-audit__line live-audit__line--" + sentence.level;
    // v3.6.0：问答高亮 —— 用户提问与 Agent 回答用**气泡**样式突出，
    //   不再跟普通流水一样只是绿字/黄字。
    var kind = auditEntryKind(entry);
    if (kind && line.classList && line.classList.add) {
      line.classList.add("live-audit__line--" + kind);
    }
    var clock = document.createElement("span");
    clock.className = "live-audit__time";
    // v3.5.1：快照里的条目自带 time（后端格式化，时区只有一个解释者）；
    // SSE 实时推来的没有 —— 那就现场按时间戳算，两条路结果一致。
    clock.textContent = "[" + auditEntryClock(entry) + "]";
    line.appendChild(clock);
    line.appendChild(document.createTextNode(sentence.text));
    el.liveAuditLines.appendChild(line);
    trimAuditLines();
    return line;
  }

  /**
   * 把一条普通流水并进「连续普通流水」那一串（v3.7.0）。
   *
   * ⚠️ 第一条先按**普通行**显示 —— 等第二条来了才合并成计数行。
   *    这样「只有一条」的时候不会平白少掉一行信息。
   *
   * @param {object} entry 流水条目。
   * @param {object} sentence 人话与等级。
   * @return {void}
   */
  function appendAuditPlainRun(entry, sentence) {
    var run = state.auditPlainRun;
    if (run) {
      run.count += 1;
      run.last = auditEntryClock(entry);
      renderAuditPlainRun(run);
      return;
    }
    var line = document.createElement("div");
    line.className = "live-audit__line live-audit__line--ok";
    var clock = document.createElement("span");
    clock.className = "live-audit__time";
    clock.textContent = "[" + auditEntryClock(entry) + "]";
    line.appendChild(clock);
    var body = document.createElement("span");
    body.className = "live-audit__body";
    body.textContent = sentence.text;
    line.appendChild(body);
    el.liveAuditLines.appendChild(line);
    trimAuditLines();
    state.auditPlainRun = {
      count: 1,
      first: auditEntryClock(entry),
      last: auditEntryClock(entry),
      line: line,
      body: body
    };
  }

  /**
   * 把「连续普通流水」那一串画成计数行（v3.7.0）。
   *
   * @param {object} run 这一串的统计信息。
   * @return {void}
   */
  function renderAuditPlainRun(run) {
    var source = run || {};
    if (!source.body || Number(source.count || 0) < 2) {
      return;
    }
    source.body.textContent = auditCountText(source);
    if (source.line && source.line.classList && source.line.classList.add) {
      source.line.classList.add("live-audit__line--count");
    }
  }

  /**
   * 砍掉流水区里超量的旧行（v3.7.0）。
   *
   * @return {void}
   */
  function trimAuditLines() {
    while (el.liveAuditLines.childElementCount > LIVE_AUDIT_LIMIT) {
      el.liveAuditLines.removeChild(el.liveAuditLines.firstElementChild);
    }
  }

  /**
   * 用服务端快照**重放**整段流水（v3.3.1）。
   *
   * 切换会话 / 刷新页面时用 —— 不然用户会看到一块**空白**的面板，
   * 误以为哨兵没在工作。
   *
   * @param {Array} entries 流水条目列表（**旧 → 新**）。
   */
  function renderLiveAudit(entries) {
    if (!el.liveAuditLines) {
      return;
    }
    var list = entries || [];
    /* v4.1.0：内容没变就**不动 DOM** ——
     *
     * 看板每 15 秒回放一次实时流水。早先的实现是「清空 → 重建整块」，
     * 于是用户正在往上翻看历史时，滚动位置被重置到顶 ——
     * 看着像「面板自己跳了」。
     *
     * 告警列表早就用「签名没变就不重建」解决了同一个问题
     * （见 renderAlerts），这里照抄那个套路。
     */
    var signature = liveAuditSignature(list);
    if (signature === state.renderedLiveAuditSig) {
      return;
    }
    /* ⚠️ 顺序要紧：``clearLiveAudit()`` 会把签名清空，
     * 所以必须**先清空、再记签名**。 */
    clearLiveAudit();
    state.renderedLiveAuditSig = signature;
    list.forEach(appendLiveAudit);
  }

  /**
   * 实时流水的「签名」。
   *
   * @param {Array} entries 流水条目列表。
   * @return {string} 签名串；序列化失败返回空串。
   */
  function liveAuditSignature(entries) {
    try {
      return JSON.stringify(entries || []);
    } catch (err) {
      /* 循环引用之类的畸形数据 —— 宁可每次重画，也不能错判成「没变」。 */
      return "";
    }
  }

  /* 切换会话：换告警列表 + 换日志行 + 换监控目标面板。 */
  function switchSession(sessionId) {
    var id = String(sessionId || "");
    if (!id || id === state.activeSession) {
      return;
    }
    state.activeSession = id;
    state.counts = {};
    state.scanCounts = {};
    state.tokens = { total_tokens: 0, estimated: false };
    // v3.5.0：虚拟会话筛选是**挂在某个会话上的**——
    // 切到别的会话还留着上一个的筛选，用户会以为「这边没有告警」。
    state.streamFilter = { sessionId: "", key: "" };
    clearAlerts();
    clearLogLines();
    clearLiveAudit();
    renderSessions();
    loadSessionContent(id);
    loadSessionTarget(id);
    loadSessionGuardian(id);
    // v2.4.0：每路会话有**自己的**任务目标 ——
    // 挂载进来的会话必须把顶栏输入框换成它自己的那一份，
    // 否则用户会在 A 会话的目标上误改 B 会话。
    loadTaskGoal();
    pushConsole("切换到会话：" + id, "event");
  }

  function loadSessionContent(sessionId) {
    var id = encodeURIComponent(String(sessionId));
    return Promise.all([
      fetch("/api/sessions/" + id + "/alerts?limit=100")
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          if (String(sessionId) !== state.activeSession) {
            return;
          }
          state.counts = (data && data.counts) || {};
          updateCounters();
          renderAlerts((data && data.alerts) || []);
        }),
      fetch("/api/sessions/" + id + "/state")
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          if (String(sessionId) !== state.activeSession) {
            return;
          }
          state.scanCounts = (data && data.scan_counts) || {};
          state.tokens = (data && data.tokens) || {};
          // v3.5.0：级联审核统计（L2 过滤数 / 省下的云端调用数）。
          state.cascade = (data && data.cascade) || state.cascade;
          state.paused = !!(data && data.paused);
          updateCounters();
          updateStateChip(data && data.status);
          // 实时审核流水（v3.3.1）——
          // 首屏 / 切换会话都要把它**回放**出来，
          // 不然用户看到的是一块空白面板，误以为哨兵没在工作。
          if (data && data.ontrack) {
            renderLiveAudit(data.ontrack);
          }
          var lines = (data && data.recent_lines) || [];
          if (lines.length) {
            clearLogLines();
            lines.forEach(appendLogLine);
          }
        })
    ]).catch(function (err) {
      pushConsole("加载会话内容失败：" + err.message, "line");
    });
  }

  function loadSessionTarget(sessionId) {
    var id = encodeURIComponent(String(sessionId));
    return fetch("/api/sessions/" + id + "/watch_target")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (String(sessionId) !== state.activeSession) {
          return;
        }
        updateTargetUi(data || {});
      })
      .catch(function (err) {
        pushConsole("读取会话目标失败：" + err.message, "line");
      });
  }

  function loadSessionGuardian(sessionId) {
    var id = encodeURIComponent(String(sessionId));
    return fetch("/api/sessions/" + id + "/guardian/status")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (String(sessionId) !== state.activeSession) {
          return;
        }
        state.guardian = data || { available: false, enabled: false };
        updateGuardianChip();
      })
      .catch(function () {
        /* 守护者状态拿不到就保持原样 —— 不影响其他功能。 */
      });
  }

  /* 会话级计数累加 —— 事件来自别的会话时也要更新它的计数。 */
  function bumpSessionCount(sessionId, verdict) {
    if (!sessionId) {
      return;
    }
    var session = findSession(sessionId);
    if (!session) {
      return;
    }
    session.counts = session.counts || {};
    session.counts[verdict] = (session.counts[verdict] || 0) + 1;
    session.alert_total = (session.alert_total || 0) + 1;
  }

  function submitTaskGoal() {
    if (!el.goalInput) {
      return;
    }
    var goal = el.goalInput.value.trim();
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) + "/task_goal"
      : "/api/task_goal";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: goal })
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          updateGoalUi(data.goal || goal);
          // v3.3.1：顶部的目标来源要**立刻**跟上 ——
          // 用户点了「设定」，就该马上看到「手动锚定」，
          // 而不是等 SSE 绕一圈才变（那一圈里气泡还杵在那儿，
          // 像是「没设成功」）。
          if (state.activeSession) {
            loadSessionTarget(state.activeSession);
          } else {
            loadWatchTarget();
          }
          showToast(
            goal
              ? "任务目标已设定：意图漂移检测已开启"
              : "任务目标已清空：意图漂移检测停用"
          );
          pushConsole("任务目标 → " + (goal || "（空）"), "event");
        } else {
          showToast((data && data.error) || "设定任务目标失败", true);
        }
      })
      .catch(function (err) {
        showToast("设定任务目标失败：" + err.message, true);
      });
  }

  function runGuardianAction(action, alert, button) {
    if (!guardianArmed()) {
      showToast("守护者未启用 —— 请在配置里把 intervention_mode 设为 guardian", true);
      return;
    }
    var label = action === "intervene" ? "强制中断" : "放行并忽略";
    if (button) {
      button.disabled = true;
      button.textContent = "执行中…";
    }
    var payload = {
      rule_id: String((alert && alert.rule_id) || ""),
      reason: String((alert && alert.reason) || ""),
      command: String((alert && alert.command) || ""),
      source: "gui"
    };
    // 多路会话：干预必须打到**这条告警所属的会话**去 ——
    // 放行 A 的进程却解除了 B 的挂起，那就乱了。
    var target = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) +
        "/guardian/" + action
      : "/api/guardian/" + action;
    fetch(target, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (result.ok && body.ok) {
          showToast(body.message || label + " 已执行");
          pushConsole(label + " · " + (body.rule_id || "-"), "event");
        } else {
          showToast(body.error || body.message || label + " 失败", true);
          pushConsole(label + " 失败：" + (body.error || body.message || "-"), "alert");
        }
        loadSessionGuardian(state.activeSession);
      })
      .catch(function (err) {
        showToast(label + " 失败：" + err.message, true);
      })
      .then(function () {
        if (button) {
          button.disabled = false;
          button.textContent = label;
        }
      });
  }

  function buildAlertActions(alert) {
    var bar = document.createElement("div");
    bar.className = "alert-actions";

    // 干预按钮：只给 DANGEROUS 的卡片 —— 那是用户真的能动手的场合。
    if (String(alert.verdict || "").toUpperCase() === "DANGEROUS") {
      var interrupt = document.createElement("button");
      interrupt.type = "button";
      interrupt.className = "btn btn--danger btn--sm";
      interrupt.textContent = "强制中断";
      interrupt.title = "挂起（或中断）被监控进程 —— 随时可以放行";
      interrupt.addEventListener("click", function () {
        runGuardianAction("intervene", alert, interrupt);
      });
      bar.appendChild(interrupt);

      var release = document.createElement("button");
      release.type = "button";
      release.className = "btn btn--warn btn--sm";
      release.textContent = "放行并忽略";
      release.title = "恢复进程，并让哨兵在当前任务中不再拦截该规则";
      release.addEventListener("click", function () {
        runGuardianAction("override", alert, release);
      });
      bar.appendChild(release);
    }

    // 【复制纠偏提示】/【复制拦截原因】——
    // 每张卡片都给：用户看到任何一条异常，都可能想直接甩给 Agent。
    bar.appendChild(buildCopyPromptButton(alert));

    // 【直接发送给 Agent】（v2.0.0）——
    // 不用人手动粘贴，直接写进 Agent 的标准输入；
    // CLI 不支持就降级到留言箱，并告诉用户下一步该干什么。
    bar.appendChild(buildSendToAgentButton(alert));

    // 【一键回滚至拦截点】（v2.0.0）——
    // 只有真的打过安全点的告警才有（没快照可回滚就是摆设）。
    var rollback = buildRollbackButton(alert);
    if (rollback) {
      bar.appendChild(rollback);
    }

    // 【查看改动】（v2.1.0）——
    // 让用户先看清「Agent 到底改了啥」，再决定放行还是回滚。
    var diffButton = buildViewDiffButton(alert);
    if (diffButton) {
      bar.appendChild(diffButton);
    }

    // 【回滚并重试】（v2.1.0）——
    // 一键复活：回滚 + 注入 + 重拉 Agent。只给红线级且有安全点的告警。
    var resume = buildResumeButton(alert);
    if (resume) {
      bar.appendChild(resume);
    }

    var hint = document.createElement("span");
    hint.className = "alert-actions__hint";
    bar.appendChild(hint);

    // 干预按钮的可用状态由 refreshGuardianButtons() 统一管理 ——
    // 在这里写死 disabled 会和「守护者状态后到」产生竞态。
    return bar;
  }

  /* ------------------------------------------- 复制纠偏提示（v1.2.0） */

  /**
   * 把一段文本复制到剪贴板。
   *
   * ⚠️ 优先用 Clipboard API；但它在**非安全上下文**里不可用
   *    （http + 非 localhost），所以留了 execCommand 的老办法兜底。
   *
   * @param {string} text 要复制的文本。
   * @return {Promise<boolean>} 是否复制成功。
   */
  function copyToClipboard(text) {
    var value = String(text || "");
    if (!value) {
      return Promise.resolve(false);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value).then(
        function () {
          return true;
        },
        function () {
          return legacyCopy(value);
        }
      );
    }
    return Promise.resolve(legacyCopy(value));
  }

  /**
   * 老办法复制（Clipboard API 不可用时的兜底）。
   *
   * @param {string} value 文本。
   * @return {boolean} 是否成功。
   */
  function legacyCopy(value) {
    try {
      var area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(area);
      return !!ok;
    } catch (err) {
      return false;
    }
  }

  /**
   * 生成并复制「纠偏提示」/「拦截原因」。
   *
   * 提示文本由**后端**生成 —— 那里才有完整的上下文
   * （context_history / drift.goal / task_goal）。
   *
   * @param {Object} alert 告警对象。
   * @param {HTMLElement} button 触发按钮（用于回显「已复制」）。
   */
  function copyCorrectionPrompt(alert, button) {
    var alertId = String((alert && alert.alert_id) || "");
    if (!alertId) {
      showToast("这条告警没有 ID，无法生成提示。", true);
      return;
    }
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) +
        "/alerts/" + encodeURIComponent(alertId) + "/correction"
      : "/api/alerts/" + encodeURIComponent(alertId) + "/correction";
    var original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "生成中…";
    }
    fetch(url, { method: "POST" })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "生成提示失败", true);
          pushConsole("生成纠偏提示失败：" + (body.error || "-"), "alert");
          return;
        }
        var prompt = String(body.prompt || "");
        copyToClipboard(prompt).then(function (copied) {
          if (copied) {
            showToast((body.label || "提示") + "已复制到剪贴板");
            pushConsole("已复制" + (body.label || "提示") + "（" +
              prompt.length + " 字）", "event");
          } else {
            showToast("复制失败，请手动选中页面上的内容。", true);
          }
        });
      })
      .catch(function (err) {
        showToast("生成提示失败：" + err.message, true);
      })
      .then(function () {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      });
  }

  /**
   * 在告警卡片上加【复制纠偏提示】按钮。
   *
   * 红线级告警的按钮文案变成【复制拦截原因】——
   * 用户要看的不是「怎么纠偏」，而是「为什么被拦」。
   *
   * @param {Object} alert 告警对象。
   * @return {HTMLElement} 按钮元素。
   */
  function buildCopyPromptButton(alert) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--ghost btn--sm alert-card__copy alert-card__aux";
    var redLine = isRedLine(alert);
    button.textContent = redLine ? "复制拦截原因" : "复制纠偏提示";
    button.title = redLine
      ? "复制「为什么被拦」+ 怎么换做法，直接粘给 Agent"
      : "复制一段纠偏提示，直接粘给 Agent 让它回到任务目标";
    button.addEventListener("click", function () {
      copyCorrectionPrompt(alert, button);
    });
    return button;
  }

  /* ------------------------------------------- 直接发送给 Agent（v2.0.0） */

  /**
   * 把纠偏提示**直接写给 Agent**（v2.0.0）。
   *
   * 与【复制纠偏提示】的区别：
   * 复制只是把文字放进剪贴板，还得用户自己粘；
   * 这里是把文字**送进 Agent 的标准输入** ——
   * Agent 能不能读到，取决于它那个 CLI 是否支持流式输入。
   *
   * 送不进去时会**降级到留言箱**：
   * 指令落盘留底，用户能自己粘，哨兵下次启动也能补投。
   * 两种情况都会在 toast 和控制台里说清楚。
   *
   * @param {Object} alert 告警对象。
   * @param {HTMLElement} button 触发按钮。
   */
  function sendToAgent(alert, button) {
    var alertId = String((alert && alert.alert_id) || "");
    if (!alertId) {
      showToast("这条告警没有 ID，无法发送。", true);
      return;
    }
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) +
        "/alerts/" + encodeURIComponent(alertId) + "/inject"
      : "/api/alerts/" + encodeURIComponent(alertId) + "/inject";
    var original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "发送中…";
    }
    fetch(url, { method: "POST" })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "发送失败", true);
          pushConsole("发送指令给 Agent 失败：" + (body.error || "-"),
            "alert");
          return;
        }
        var channel = String(body.channel || "");
        var detail = String(body.detail || "已送达。");
        var next = String(body.next_step || "");
        showToast(detail);
        pushConsole("指令已送达（渠道：" + channel + "）" + detail, "event");
        if (next) {
          pushConsole(next, "line");
        }
      })
      .catch(function (err) {
        showToast("发送失败：" + err.message, true);
      })
      .then(function () {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      });
  }

  /**
   * 在告警卡片上加【直接发送给 Agent】按钮。
   *
   * @param {Object} alert 告警对象。
   * @return {HTMLElement} 按钮元素。
   */
  function buildSendToAgentButton(alert) {
    var button = document.createElement("button");
    button.type = "button";
    button.className =
      "btn btn--primary btn--sm alert-card__send alert-card__aux";
    var redLine = isRedLine(alert);
    button.textContent = redLine ? "发送拦截原因" : "发送给 Agent";
    button.title = redLine
      ? "把「为什么被拦」直接写进 Agent 的标准输入（不支持则写进留言箱）"
      : "把纠偏提示直接写进 Agent 的标准输入（不支持则写进留言箱）";
    button.addEventListener("click", function () {
      sendToAgent(alert, button);
    });
    return button;
  }

  /* ------------------------------------------- 一键回滚（v2.0.0） */

  /**
   * 【一键回滚至拦截点】（v2.0.0）。
   *
   * 先问一次「要恢复哪些文件、删掉哪些新增文件」，
   * 用户点头才动手 —— 权限小于用户。
   *
   * @param {Object} alert 告警对象。
   * @param {HTMLElement} button 触发按钮。
   */
  function rollbackToSafePoint(alert, button) {
    var alertId = String((alert && alert.alert_id) || "");
    if (!alertId) {
      showToast("这条告警没有 ID，无法回滚。", true);
      return;
    }
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) +
        "/alerts/" + encodeURIComponent(alertId) + "/rollback"
      : "/api/alerts/" + encodeURIComponent(alertId) + "/rollback";
    var original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "回滚中…";
    }
    fetch(url, { method: "POST" })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "回滚失败", true);
          pushConsole("回滚失败：" + (body.error || "-"), "alert");
          return;
        }
        var restored = Number(body.restored_count || 0);
        var deleted = Number(body.deleted_count || 0);
        var elapsed = Number(body.elapsed || 0);
        showToast(
          "已回滚：恢复 " + restored + " 个文件，删除 " + deleted +
          " 个新增文件（耗时 " + elapsed.toFixed(2) + " 秒）"
        );
        pushConsole(
          "回滚完成：恢复 " + restored + " 个、删除 " + deleted +
          " 个，耗时 " + elapsed.toFixed(2) + " 秒",
          "event"
        );
      })
      .catch(function (err) {
        showToast("回滚失败：" + err.message, true);
      })
      .then(function () {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      });
  }

  /**
   * 在告警卡片上加【一键回滚至拦截点】按钮。
   *
   * 只有**真的打过安全点**的告警才有这个按钮 ——
   * 没快照可回滚，按钮就是摆设。
   *
   * @param {Object} alert 告警对象。
   * @return {HTMLElement|null} 按钮元素；没有安全点时返回 ``null``。
   */
  function buildRollbackButton(alert) {
    var safePoint = (alert && alert.safe_point) || null;
    if (!safePoint || !safePoint.id) {
      return null;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className =
      "btn btn--warn btn--sm alert-card__rollback alert-card__aux";
    button.textContent = "一键回滚至拦截点";
    var files = Number(safePoint.files_count || 0);
    var mode = String(safePoint.mode || "full");
    button.title =
      "把工作目录恢复到这次拦截发生时的安全点" +
      "（" + files + " 个文件 · " +
      (mode === "full" ? "全量快照" : "增量快照") + "）";
    button.addEventListener("click", function () {
      rollbackToSafePoint(alert, button);
    });
    return button;
  }

  /* ------------------------------------------- 查看改动（v2.1.0） */

  /**
   * 弹出一个简易的模态框。
   *
   * ⚠️ 自己建 DOM 而不是用 ``<dialog>`` ——
   *    老版本 Edge / WebView 上 ``<dialog>`` 的支持并不一致。
   *
   * @param {string} title 标题。
   * @param {HTMLElement} body 内容节点。
   * @param {Array} buttons 底部按钮数组（``{label, kind, onClick}``）。
   */
  function openModal(title, body, buttons) {
    closeModal();
    var overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.id = "sentinel-modal";

    var box = document.createElement("div");
    box.className = "modal__box";

    var head = document.createElement("div");
    head.className = "modal__head";
    var heading = document.createElement("h2");
    heading.className = "modal__title";
    heading.textContent = title;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "modal__close";
    close.textContent = "×";
    close.title = "关闭";
    close.addEventListener("click", closeModal);
    head.appendChild(heading);
    head.appendChild(close);

    var content = document.createElement("div");
    content.className = "modal__body";
    content.appendChild(body);

    var foot = document.createElement("div");
    foot.className = "modal__foot";
    (buttons || []).forEach(function (item) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn--sm " + (item.kind || "");
      button.textContent = item.label;
      button.addEventListener("click", function () {
        if (item.onClick) {
          item.onClick();
        }
      });
      foot.appendChild(button);
    });

    box.appendChild(head);
    box.appendChild(content);
    box.appendChild(foot);
    overlay.appendChild(box);
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) {
        closeModal();
      }
    });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onModalKey);
    return overlay;
  }

  function onModalKey(event) {
    if (event.key === "Escape") {
      closeModal();
    }
  }

  /* v4.0.1：进程树扫描的轮询定时器（外层作用域）。 */
  var processPollTimer = null;

  /* v4.1.0：进程活了多久却一直没输出，才算「慢/待验证」。
   *
   * v4.2.0：阈值 60 秒 → **10 秒**。
   *
   * 为什么改：任务要求「进程启动后 10 秒内没有输出任何内容，
   * 就不要显示为运行中，而是提示『疑似挂起』」。
   * 10 秒是「这个 CLI 还没吐出第一行」的合理上限 ——
   * 连启动都超过 10 秒的 CLI，用户等下去已经没有意义了。
   *
   * ⚠️ 取舍：v4.1.0 曾按外部插件实测的「未登录时静默挂起 40 秒以上」的
   * 实测取 60 秒，误报更少但**发现太晚**。10 秒更灵敏，
   * 代价是「启动慢的 CLI」可能被提前标黄 —— 所以这一档是
   * **灰色**（不是警示色），提示文案写「疑似挂起」而不是断言。
   */
  var SLOW_PROCESS_SECONDS = 10;

  /**
   * 停掉「正在深度检索进程…」的轮询。
   *
   * ⚠️ 模态框关掉就必须停 —— 不然会在后台一直打接口。
   */
  function clearProcessPoll() {
    if (processPollTimer !== null) {
      window.clearTimeout(processPollTimer);
      processPollTimer = null;
    }
  }

  /** 关掉模态框。 */
  function closeModal() {
    var existing = document.getElementById("sentinel-modal");
    if (existing && existing.parentNode) {
      existing.parentNode.removeChild(existing);
    }
    document.removeEventListener("keydown", onModalKey);
    clearProcessPoll();
  }

  /* ------------------------------------------------------ 雷达挂载（v2.3.0） */

  /* 工作区来源的中文说明（v3.0.1）。
   *
   * ⚠️ 「cwd」不显示 —— 那是**正常**情况，显示出来只会让人以为有异常。
   */
  var WORKSPACE_SOURCE_LABELS = {
    args: "工作区来自启动参数",
    process: "工作区来自进程",
    file: "仅文件名（未提取到工作区）"
  };

  /**
   * 把工作区来源翻成一句话（没有特殊来源就返回空串）。
   *
   * @param {string} source 后端给的来源标记。
   * @return {string} 说明文本。
   */
  function workspaceSourceLabel(source) {
    var key = String(source || "");
    if (!key) {
      return "";
    }
    return WORKSPACE_SOURCE_LABELS[key] || "";
  }

  /**
   * 把 epoch 秒格式化成短时间（v3.0.1）。
   *
   * @param {number} stamp epoch 秒。
   * @return {string} 形如「今天 16:49」；非法值返回空串。
   */
  function formatStamp(stamp) {
    var value = Number(stamp);
    if (!isFinite(value) || value <= 0) {
      return "";
    }
    var date = new Date(value * 1000);
    var now = new Date();
    var clock = ("0" + date.getHours()).slice(-2) + ":" +
      ("0" + date.getMinutes()).slice(-2);
    var sameDay = date.getFullYear() === now.getFullYear() &&
      date.getMonth() === now.getMonth() &&
      date.getDate() === now.getDate();
    if (sameDay) {
      return "今天 " + clock;
    }
    return (date.getMonth() + 1) + "月" + date.getDate() + "日 " + clock;
  }

  /* --------------------------------------------------------- 活跃进程感知 */
  //
  // v3.3.2 —— 雷达扫的是**文件**，这一路扫的是**进程**。
  //
  // 用户最常问的一句话是「我明明在终端跑着 Agent，看板为什么抓不到？」
  // 答案往往是：Agent 的输出没被重定向成文件，雷达扫不到。
  //
  // ⚠️ 这一条**只能提示，不能自动接管**：
  //   哨兵是只读监控（红线 R1 / R2），挂不挂是用户的事。

  /* 提示的两种句式（v3.3.2）——
   *
   * 写成**常量**而不是散在逻辑里的字符串拼接 ——
   * 这样「用户到底会看到哪句话」有一处可以钉住的地方。
   *
   * ⚠️ 单独一种 Agent 时用**产品名**（用户认识「MCode」）；
   *   混了好几种才写总数 + 分项，免得用户自己去数。 */
  var RADAR_ALERT_SINGLE =
    "⚠️ 检测到本机有 {count} 个活跃的 {agent} 进程，未发现可读日志。";
  var RADAR_ALERT_MULTI =
    "⚠️ 检测到本机有 {count} 个活跃的 Agent 进程（{detail}），未发现可读日志。";

  /**
   * 把「哪些 Agent 在跑、哪些没被挂上」拼成一句话。
   *
   * @param {Object} data 后端 ``/api/process_activity`` 的返回。
   * @return {string} 提示标题；不需要提示时返回空串。
   */
  function radarAlertText(data) {
    if (!data || data.available === false) {
      return "";
    }
    var count = Number(data.unmonitored_count || 0);
    if (!count) {
      return "";
    }
    var byAgent = data.by_agent || {};
    var keys = Object.keys(byAgent).sort();
    if (!keys.length) {
      return "";
    }
    if (keys.length === 1) {
      return RADAR_ALERT_SINGLE
        .split("{count}").join(String(count))
        .split("{agent}").join(agentLabel(keys[0]));
    }
    var parts = keys.map(function (key) {
      return agentLabel(key) + " " + byAgent[key] + " 个";
    });
    return RADAR_ALERT_MULTI
      .split("{count}").join(String(count))
      .split("{detail}").join(parts.join("、"));
  }

  /**
   * 渲染左侧的「活跃进程未挂载」提示。
   *
   * @param {Object} data 后端返回；为空或无异常时收起提示。
   */
  function renderRadarAlert(data) {
    // v3.5.1：雷达**一个活跃 Agent 都没扫到**时，
    // 左侧要给出「怎么开始监控」的引导（而不是干瞪眼）。
    updateRadarEmpty(data ? Number(data.active_count || 0) : 0);
    if (!el.radarAlert) {
      return;
    }
    var text = radarAlertText(data);
    if (!text) {
      el.radarAlert.hidden = true;
      if (el.radarAlertTitle) {
        el.radarAlertTitle.textContent = "";
      }
      return;
    }
    if (el.radarAlertTitle) {
      el.radarAlertTitle.textContent = text;
    }
    el.radarAlert.hidden = false;
  }

  /**
   * 拉一次「本机活跃 Agent 进程」状态并渲染。
   *
   * 后端带 8 秒缓存，所以这里轮询得再勤也不贵。
   */
  function loadProcessActivity() {
    return fetch("/api/process_activity")
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.capture_path) {
          state.radarCapturePath = String(data.capture_path);
        }
        // v3.6.0：捕获文件**在不在** —— 空态命令与提示语都靠它。
        state.radarCaptureExists = !!(data && data.capture_exists);
        renderRadarAlert(data);
      })
      .catch(function (err) {
        console.error("拉取活跃进程状态失败：", err);
      });
  }

  /**
   * 打开「发现 / 挂载进程」面板。
   *
   * 哨兵的新定位是**雷达**（v2.3.0）——
   * 用户早就在自己的终端里跑着 Agent 了，
   * 哨兵不该要求「必须从我这儿启动」，
   * 而应该能**发现**它们、把日志接进来实时审查。
   *
   * 两条路：
   *
   * 1. **发现**：扫描 ``~/.codex`` / ``~/.mcode`` / ``~/.claude``
   *    里正在被活跃写入的日志，点一下就接进来；
   * 2. **管道**：用户自己重定向了输出（``mcode ... > session.jsonl``），
   *    直接把路径粘进来。
   *
   * @param {boolean} focusManual 是否把光标直接送到「手动填路径」的输入框。
   *
   *   v3.3.2：雷达提示「检测到活跃进程但没挂载」时，用户点提示框的
   *   下一个动作就是**手动填日志路径** —— 所以那一条路径上要把光标
   *   直接送到输入框，别让他自己找。
   */
  function openAttachModal(focusManual) {
    var body = document.createElement("div");
    body.className = "attach";

    var hint = document.createElement("p");
    hint.className = "attach__hint";
    hint.textContent =
      "哨兵正在扫描你机器上各个 AI CLI 的会话目录，找出正在被写入的日志" +
      "（默认只列活跃会话，历史记录收在下面的复选框里）。" +
      "点「接入」就能实时审查。";
    body.appendChild(hint);

    var manual = document.createElement("div");
    manual.className = "attach__manual";
    var manualLabel = document.createElement("label");
    manualLabel.className = "attach__label";
    manualLabel.textContent = "或者直接指定日志路径";
    var manualRow = document.createElement("div");
    manualRow.className = "attach__row";
    var manualInput = document.createElement("input");
    manualInput.type = "text";
    manualInput.className = "attach__input mono";
    manualInput.placeholder = "C:\\Users\\你\\.codex\\sessions\\…\\rollout.jsonl";
    var manualButton = document.createElement("button");
    manualButton.type = "button";
    manualButton.className = "btn btn--primary btn--sm";
    manualButton.textContent = "接入";
    manualRow.appendChild(manualInput);
    manualRow.appendChild(manualButton);
    manual.appendChild(manualLabel);
    manual.appendChild(manualRow);
    // v3.6.0：路径校验的红字提示 ——
    //   文件不存在就**当场**拦下来，别让他等一个 500 式的报错。
    var manualError = document.createElement("p");
    manualError.className = "attach__error";
    manualError.hidden = true;
    manual.appendChild(manualError);
    body.appendChild(manual);

    var status = document.createElement("p");
    status.className = "attach__status";
    status.textContent = "正在扫描…";
    body.appendChild(status);

    var list = document.createElement("div");
    list.className = "attach__list";
    body.appendChild(list);

    /* v4.0.0：外部插件派发**进程树**的发现结果 ——
       和上面的「会话列表」是**两回事**：
       会话列表扫的是**日志文件**；这里扫的是**进程** ——
       外部插件的桥接宿主派发出来的 Agent CLI。 */
    var procList = document.createElement("div");
    procList.className = "attach__procs";
    body.appendChild(procList);

    /**
     * 真正发起挂载。
     *
     * @param {string} path 日志路径。
     * @param {string} agent Agent 标识（v4.0.0：进程树发现时带上来）。
     * @param {string} cwd 工作目录（v4.0.0：进程树发现时带上来）。
     */
    function doAttach(path, agent, cwd) {
      if (!path) {
        return;
      }
      status.className = "attach__status attach__status--busy";
      status.textContent = "正在接入 " + path + " …";
      var payload = { path: path };
      if (agent) {
        payload.agent = String(agent);
      }
      if (cwd) {
        payload.cwd = String(cwd);
      }
      fetch("/api/attach", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function (res) {
          return res.json().then(function (data) {
            return { status: res.status, data: data };
          });
        })
        .then(function (result) {
          var data = result.data || {};
          if (!data.ok) {
            status.className = "attach__status attach__status--error";
            status.textContent = "接入失败：" + String(data.error || "未知原因");
            return;
          }
          status.className = "attach__status attach__status--ok";
          status.textContent = String(data.message || "已接入。");
          showToast("已接入 " + String(data.message || path));
          loadSessions();
        })
        .catch(function (err) {
          status.className = "attach__status attach__status--error";
          status.textContent = "接入失败：" + err.message;
        });
    }

    /**
     * 在输入框下面显示（或者收起）红字提示（v3.6.0）。
     *
     * @param {string} message 提示文字；空串表示收起。
     * @return {void}
     */
    function showManualError(message) {
      var text = String(message || "");
      manualError.textContent = text;
      manualError.hidden = !text;
    }

    /**
     * 接入**之前**先问后端：这个文件到底在不在（v3.6.0）。
     *
     * ⚠️ 为什么要多这一趟：
     *   v3.5.2 的流程是「直接 POST /api/attach」，
     *   文件不存在时后端回 400，弹窗顶上一句
     *   「接入失败：文件不存在：xxx」——
     *   用户根本不知道**该改哪里**，更不知道文件还没生成。
     *   现在前端先确认一遍，不存在就直接在输入框下面飘红字。
     *
     * @param {string} path 用户填的路径。
     * @return {void}
     */
    function verifyPathThenAttach(path) {
      var target = String(path || "").trim();
      if (!target) {
        showManualError("请先填写日志文件路径。");
        return;
      }
      showManualError("");
      manualButton.disabled = true;
      fetch("/api/path_exists?path=" + encodeURIComponent(target))
        .then(function (res) {
          return res.json();
        })
        .then(
          function (data) {
            if (data && data.exists) {
              doAttach(target);
              return;
            }
            showManualError("文件不存在，请检查路径或重新生成");
          },
          function (err) {
            // ⚠️ 校验接口自己出毛病时**不阻断**用户 ——
            //   退回到原来的流程，让后端去报错。
            console.warn("路径校验失败，改用直接接入：", err);
            doAttach(target);
          }
        )
        .then(function () {
          manualButton.disabled = false;
        });
    }

    manualButton.addEventListener("click", function () {
      verifyPathThenAttach(manualInput.value);
    });
    manualInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        verifyPathThenAttach(manualInput.value);
      }
    });
    manualInput.addEventListener("input", function () {
      showManualError("");
    });

    openModal("发现 / 挂载进程", body, []);

    /* v3.3.2：从「活跃进程未挂载」提示点进来的话，
     * 光标直接落在手动路径输入框上 —— 用户下一步就是粘贴路径。
     *
     * 顺手把「快捷重定向命令」的默认落盘路径预填进去：
     * 用户多半就是照那条命令跑的，路径能对上就少一次手敲。 */
    if (focusManual === true) {
      if (state.radarCapturePath) {
        manualInput.value = state.radarCapturePath;
      }
      try {
        manualInput.focus();
        manualInput.select();
      } catch (err) {
        console.error("聚焦手动路径输入框失败：", err);
      }
    }

    /* 全量结果 —— 勾了「显示其余」才用得上。
     *
     * ⚠️ v3.0.2：默认只请求「正在写入」的（``active_only=1``）。
     *   服务端把静默文件**连读都不读**（v3.0.2 的性能修复），
     *   所以这一路只花几十毫秒。
     */
    var fullItems = null;

    /**
     * 拉一次雷达结果。
     *
     * @param {number} activeOnly 1 = 只要正在写入的；0 = 全量。
     * @return {Promise<Object>} 后端返回的 JSON。
     */
    function fetchDiscover(activeOnly) {
      return fetch("/api/discover?active_only=" + activeOnly)
        .then(function (res) {
          return res.json();
        });
    }

    /**
     * 轮询进程树扫描结果（v4.0.1）。
     *
     * ⚠️ 为什么要有轮询 —— 服务端的扫描**最多只等 1.5 秒**
     *    （超时熔断），超时就先返回已经扫到的那部分，
     *    后台继续扫。界面上要如实告诉用户「正在深度检索进程…」，
     *    并把后续结果补上来。
     *
     * ⚠️ 只在服务端说「还在扫描」时才继续轮 —— 扫完就不该再打接口。
     *
     * @param {Function} onDone 拿到最终结果后的回调。
     */
    function pollProcessScan(onDone) {
      clearProcessPoll();
      processPollTimer = window.setTimeout(function () {
        processPollTimer = null;
        /* 模态框关了就别再查了。 */
        if (!document.getElementById("sentinel-modal")) {
          return;
        }
        fetchDiscover(1)
          .then(function (data) {
            var rows = (data && data.processes) || [];
            var scanStatus =
              String((data && data.process_scan_status) || "");
            renderProcesses(rows, scanStatus);
            if (scanStatus === "scanning") {
              pollProcessScan(onDone);
              return;
            }
            if (onDone) {
              onDone(data);
            }
          })
          .catch(function () {
            /* 轮询失败就停 —— 反复打接口只会更吵。 */
          });
      }, 800);
    }

    /**
     * 渲染一列条目。
     *
     * @param {Array} rows 要展示的条目。
     */
    function renderRows(rows) {
      while (list.firstElementChild) {
        list.removeChild(list.firstElementChild);
      }
      if (!rows.length) {
        // v3.5.1：扫不到活跃 Agent 时**别只说「没发现」** ——
        // 直接把可执行的那一条命令摆出来，再给一键复制。
        var empty = document.createElement("div");
        empty.className = "attach__empty";
        empty.appendChild(buildRadarEmptyGuide());
        var tail = document.createElement("p");
        tail.className = "attach__empty-tail";
        tail.textContent =
          "如果 Agent 已经在跑、只是把输出重定向到了别处，" +
          "把日志路径贴进上面的路径框即可。";
        empty.appendChild(tail);
        list.appendChild(empty);
        return;
      }
      rows.forEach(function (item) {
        var row = document.createElement("div");
        row.className = "attach__item";
        if (item.attached) {
          row.classList.add("attach__item--attached");
        }

        var name = document.createElement("div");
        name.className = "attach__name";
        name.textContent = String(item.label || item.path);
        row.appendChild(name);

        var meta = document.createElement("div");
        meta.className = "attach__meta mono";
        meta.textContent = String(item.path || "");
        row.appendChild(meta);

        /* 会话 ID + 首条消息时间 + 工作区来源（v3.0.1）——
           让用户一眼认出「这是我刚才跑的那个任务」。 */
        var ident = document.createElement("div");
        ident.className = "attach__ident mono";
        var bits = [];
        if (item.session_id) {
          bits.push("会话 " + String(item.session_id).slice(0, 12));
        }
        if (item.first_time) {
          bits.push(formatStamp(item.first_time));
        }
        var source = workspaceSourceLabel(item.workspace_source);
        if (source) {
          bits.push(source);
        }
        ident.textContent = bits.join("　·　");
        if (bits.length) {
          row.appendChild(ident);
        }

        /* v3.5.2：自动拔出来的**任务锚点** ——
           哨兵扫描时已经把用户最初那句 Prompt 从日志里挖出来了，
           挂载进去就能直接开漂移监控。
           所以这里**提前告诉用户**「不用你手敲」——
           免得他还以为得自己抄一遍。 */
        var goalText = String(item.task_goal || "");
        if (goalText) {
          var goalLine = document.createElement("div");
          goalLine.className = "attach__goal";
          var goalTag = document.createElement("span");
          goalTag.className = "attach__goal-tag";
          goalTag.textContent = "[自动提取]";
          var goalBody = document.createElement("span");
          goalBody.className = "attach__goal-text";
          goalBody.textContent = goalText;
          goalBody.title = goalText;
          goalLine.appendChild(goalTag);
          goalLine.appendChild(goalBody);
          row.appendChild(goalLine);
        }

        var badge = document.createElement("span");
        badge.className = "attach__badge";
        badge.textContent = item.active ? "正在写入" : "已静默";
        badge.dataset.active = item.active ? "1" : "0";

        /* 压缩日志（DeepSeek 的 zstd）—— 只能被发现，
           **暂不支持接入实时审查**。按钮直接标出来，
           不做点下去才发现的半成品。 */
        var badges = document.createElement("div");
        badges.className = "attach__badges";
        badges.appendChild(badge);
        if (item.compressed) {
          var zstdTag = document.createElement("span");
          zstdTag.className = "attach__badge attach__badge--zstd";
          zstdTag.textContent = "zstd 压缩 · 暂不支持接入";
          badges.appendChild(zstdTag);
        }
        row.appendChild(badges);

        var button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--sm btn--primary";
        var blocked = !item.attachable;
        button.textContent = blocked
          ? "怎么接入？"
          : (item.attached ? "已接入" : "接入");
        button.disabled = !!item.attached;
        if (blocked) {
          /* ⚠️ 不能用 ``disabled`` —— 禁用按钮**收不到 click 事件**，
             用户点了没反应，只会觉得「这界面坏了」。
             改成「能点、点了给指引」（v3.1.0）。 */
          button.classList.add("btn--blocked");
          button.setAttribute("aria-disabled", "true");
          button.title = "压缩日志只能被发现，暂不支持接入实时审查";
        }
        button.addEventListener("click", function () {
          if (blocked) {
            toggleGuide(row);
            return;
          }
          button.disabled = true;
          button.textContent = "接入中…";
          doAttach(String(item.path || ""));
        });
        row.appendChild(button);
        list.appendChild(row);
      });
    }

    /**
     * 进程已经跑了多久（秒）。
     *
     * v4.1.0：读服务端给的 ``started_at``（进程创建时刻）。
     * 读不到就返回 0 —— **不知道就说不知道**，
     * 不能拿 0 去冒充「刚启动」。
     *
     * @param {object} item 进程发现结果。
     * @return {number} 已运行秒数；读不到是 0。
     */
    function slowSeconds(item) {
      var started = Number((item && item.started_at) || 0);
      if (!started) {
        return 0;
      }
      var alive = Date.now() / 1000 - started;
      return alive > 0 ? Math.round(alive) : 0;
    }

    /**
     * 这个进程算不算「慢/待验证」。
     *
     * 判据：**进程还活着，但一直没找到它写出的日志**，
     * 而且已经活了超过阈值（v4.2.0 起为 10 秒）。
     *
     * ⚠️ 阈值从 60 秒降到 10 秒（v4.2.0）—— 灵敏度优先，
     * 误报由「灰色 + 疑似挂起」的措辞来兜，不是断言。
     *
     * @param {object} item 进程发现结果。
     * @return {boolean} 算「慢」返回 true。
     */
    function isSlowProcess(item) {
      if (!item || item.attachable || item.attached) {
        return false;
      }
      var alive = slowSeconds(item);
      return alive > 0 && alive >= SLOW_PROCESS_SECONDS;
    }

    /**
     * 渲染「外部插件派发进程树」的发现结果（v4.0.0）。
     *
     * ⚠️ 这一段和上面的「会话列表」是**两回事**：
     *   会话列表扫的是**日志文件**；这里扫的是**进程** ——
     *   外部插件的桥接宿主（``python -m webui.server`` 或插件数据目录下的进程）
     *   派发出来的 Agent CLI。
     *
     * ⚠️ **没有日志就不能挂载**（红线 R7）——
     *   进程认出来了，但它的日志文件还没写出来，
     *   哨兵就**没有**可监控的目标，这时只能如实告诉用户，
     *   不能拿一个猜出来的路径去武装守护者。
     *
     * @param {Array} rows 进程发现结果。
     * @param {string} scanStatus 服务端的扫描状态
     *   （``scanning`` 表示「结果还在补」）。
     */
    function renderProcesses(rows, scanStatus) {
      while (procList.firstElementChild) {
        procList.removeChild(procList.firstElementChild);
      }
      var items = rows || [];
      var scanning = String(scanStatus || "") === "scanning";
      if (!items.length && !scanning) {
        return;
      }

      var title = document.createElement("p");
      title.className = "attach__procs-title";
      if (scanning) {
        /* v4.0.1：扫描还没完 —— 如实说「还在找」，
           别让用户以为「一个进程都没有」。 */
        title.textContent =
          "正在深度检索进程…（已找到 " + items.length + " 个）";
      } else {
        title.textContent =
          "检测到的外部进程（" + items.length + " 个正在跑）";
      }
      procList.appendChild(title);

      items.forEach(function (item) {
        var row = document.createElement("div");
        row.className = "attach__item attach__item--proc";

        var name = document.createElement("div");
        name.className = "attach__name";
        name.textContent =
          String(item.agent_label || item.agent || "未知 Agent") +
          "（pid " + String(item.pid || "?") + "）";
        row.appendChild(name);

        var meta = document.createElement("div");
        meta.className = "attach__meta mono";
        meta.textContent = String(item.cmdline || "");
        row.appendChild(meta);

        var ident = document.createElement("div");
        ident.className = "attach__ident mono";
        var bits = [];
        bits.push("宿主 pid " + String(item.host_pid || "?"));
        bits.push("第 " + String(item.depth || 1) + " 层");
        if (item.cwd) {
          bits.push(String(item.cwd));
        }
        ident.textContent = bits.join("　·　");
        row.appendChild(ident);

        var badges = document.createElement("div");
        badges.className = "attach__badges";
        var badge = document.createElement("span");
        badge.className = "attach__badge";
        if (item.attached) {
          badge.textContent = "已接入";
        } else if (item.attachable) {
          badge.textContent = "运行中 · 可接入";
        } else if (isSlowProcess(item)) {
          /* v4.1.0：进程活着却迟迟没有输出 —— **单独一档**。
           *
           * 为什么必须分开：外部插件那边实测过有的 CLI 未登录时
           * 会「静默挂起」—— stdout / stderr 全空，
           * 面板上看着像「在干活」。不单独标出来，
           * 用户会一直等一个永远不会来的结果。
           *
           * v4.2.0：阈值降到 10 秒，并把措辞改成「疑似挂起」——
           * 10 秒只能说明「还没动静」，**不能断言它挂了**，
           * 所以用灰色而不是警示色，文案也留余地。
           */
          badge.className = "attach__badge attach__badge--slow";
          badge.textContent =
            "慢/待验证 · 疑似挂起 · " + String(slowSeconds(item)) + "s";
          badge.title =
            "进程还活着，但启动 " + String(SLOW_PROCESS_SECONDS) +
            " 秒后仍没找到它写出的日志 —— 疑似挂起。\n" +
            "可能是：① 这个 CLI 未登录时静默挂起（实测有的 CLI 会挂 40 秒以上）；\n" +
            "② 它还在初始化（CLI 启动慢）；\n" +
            "③ 它压根不写日志文件，只是 stdout 输出。\n" +
            "别当成「在干活」一直等；灰标只是提醒，不是断言。";
        } else {
          badge.textContent = "运行中 · 暂无日志可接入";
        }
        badges.appendChild(badge);
        row.appendChild(badges);

        var button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--sm btn--primary";
        if (item.attached) {
          button.textContent = "已接入";
          button.disabled = true;
        } else if (item.attachable) {
          button.textContent = "接入";
          button.addEventListener("click", function () {
            button.disabled = true;
            button.textContent = "接入中…";
            doAttach(
              String(item.log_path || ""),
              String(item.agent || ""),
              String(item.cwd || "")
            );
          });
        } else {
          /* ⚠️ 不用 ``disabled`` —— 禁用按钮收不到 click 事件，
             用户点了没反应只会觉得「这界面坏了」。 */
          button.textContent = "怎么接入？";
          button.classList.add("btn--blocked");
          button.setAttribute("aria-disabled", "true");
          button.title =
            "这个进程还没写出日志文件，哨兵没有可监控的目标（红线 R7）";
        }
        row.appendChild(button);
        procList.appendChild(row);
      });
    }

    /**
     * 弹出 / 收起「压缩日志怎么接入」的指引气泡（v3.1.0）。
     *
     * ⚠️ 为什么要有这个
     * ==================
     * v3.0.2 把压缩日志的按钮**禁用**了 —— 诚实，但没用：
     * 用户看到「不能接入」只知道自己不行，不知道**怎么才行**。
     * 所以这里把话说全：压缩格式接不了，
     * 但可以在启动 Agent 时**加输出重定向**。
     *
     * @param {HTMLElement} row 这一行的容器。
     */
    function toggleGuide(row) {
      var old = row.querySelector(".attach__guide");
      if (old) {
        row.removeChild(old);
        return;
      }
      var guide = document.createElement("div");
      guide.className = "attach__guide";
      guide.textContent =
        "此 Agent 使用压缩格式，无法实时跟随。" +
        "请在启动 Agent 时添加输出重定向，例如：" +
        " 你的命令 > D:\\logs\\session.jsonl ，" +
        "然后通过上方的【手动指定日志路径】接入。";
      row.appendChild(guide);
    }

    /**
     * 画「显示其余」的复选框（有东西要折叠时才出现）。
     *
     * @param {number} hidden 折叠起来的条目数。
     * @param {Array} active 当前正在展示的条目。
     */
    function renderToggle(hidden, active) {
      var old = body.querySelector(".attach__toggle");
      if (old) {
        body.removeChild(old);
      }
      if (hidden <= 0) {
        return;
      }
      var toggle = document.createElement("label");
      toggle.className = "attach__toggle";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.id = "attach-show-all";
      var boxText = document.createElement("span");
      boxText.className = "attach__toggle-label";
      boxText.textContent = "显示其余 " + hidden + " 个文件（已静默 / 附属）";
      toggle.appendChild(box);
      toggle.appendChild(boxText);
      body.insertBefore(toggle, list);
      box.addEventListener("change", function () {
        if (!box.checked) {
          renderRows(active);
          return;
        }
        if (fullItems) {
          renderRows(fullItems);
          return;
        }
        boxText.textContent = "正在加载其余文件…";
        fetchDiscover(0)
          .then(function (data) {
            fullItems = (data && data.sessions) || [];
            renderRows(fullItems);
          })
          .catch(function (err) {
            boxText.textContent = "加载失败：" + err.message;
          });
      });
    }

    /* 第一步：只要「正在写入」的 —— 服务端会跳过所有静默文件，
       这一路只花几十毫秒。 */
    fetchDiscover(1)
      .then(function (data) {
        var items = (data && data.sessions) || [];
        var roots = (data && data.roots) || [];
        var cwd = String((data && data.cwd) || "");

        renderRows(items);
        var processes = (data && data.processes) || [];
        var scanStatus = String((data && data.process_scan_status) || "");
        renderProcesses(processes, scanStatus);
        status.className = "attach__status";
        status.textContent =
          "发现 " + items.length + " 个正在写入的会话" +
          (processes.length
            ? "、" + processes.length + " 个外部进程。"
            : "。");
        if (scanStatus === "scanning") {
          /* v4.0.1：扫描还没完（服务端 1.5 秒超时熔断）——
             如实告诉用户「正在深度检索进程…」，
             并轮询把后续结果补上来。 */
          status.textContent += " 正在深度检索进程…";
          pollProcessScan(function (final) {
            var finalRows = (final && final.processes) || [];
            renderProcesses(
              finalRows,
              String((final && final.process_scan_status) || "")
            );
            status.textContent =
              "发现 " + items.length + " 个正在写入的会话" +
              (finalRows.length
                ? "、" + finalRows.length + " 个外部进程。"
                : "。");
          });
        }

        /* 当前工作区（v3.0.2）—— 任务书要求「明确列出当前的
           cwd 工作区」，用户得一眼看出自己站在哪儿。 */
        if (cwd) {
          var cwdNote = document.createElement("p");
          cwdNote.className = "attach__roots mono";
          cwdNote.textContent = "当前工作区：" + cwd;
          body.appendChild(cwdNote);
        }
        if (roots.length) {
          var rootNote = document.createElement("p");
          rootNote.className = "attach__roots mono";
          rootNote.textContent = "扫描目录：" + roots.join("　|　");
          body.appendChild(rootNote);
        }

        /* 第二步：后台拉全量，只为了知道「其余 N 个」是几个。
           勾上复选框时直接用这份缓存，不用再等一次。 */
        return fetchDiscover(0)
          .then(function (all) {
            var allItems = (all && all.sessions) || [];
            fullItems = allItems;
            renderToggle(allItems.length - items.length, items);
          })
          .catch(function () {
            renderToggle(0, items);
          });
      })
      .catch(function (err) {
        status.className = "attach__status attach__status--error";
        status.textContent = "扫描失败：" + err.message;
      });
  }

  /**
   * 把一个文件的 unified diff 渲染成 DOM。
   *
   * 绿底 = 新增，红底 = 删除，蓝底 = 块头（``@@``）。
   *
   * @param {Object} detail 后端给的单文件差异。
   * @return {HTMLElement} 差异块。
   */
  function renderDiffFile(detail) {
    var wrap = document.createElement("div");
    wrap.className = "diff__file";
    var path = document.createElement("div");
    path.className = "diff__path";
    path.textContent =
      detail.path + "  （+" + Number(detail.added || 0) +
      " / -" + Number(detail.removed || 0) + "）";
    wrap.appendChild(path);
    var pre = document.createElement("pre");
    pre.className = "diff__body";
    (detail.lines || []).forEach(function (line) {
      var span = document.createElement("span");
      span.className = "diff__line";
      var head = String(line).slice(0, 1);
      if (head === "+" && String(line).slice(0, 3) !== "+++") {
        span.className += " diff__line--add";
      } else if (head === "-" && String(line).slice(0, 3) !== "---") {
        span.className += " diff__line--del";
      } else if (head === "@") {
        span.className += " diff__line--hunk";
      } else {
        span.className += " diff__line--ctx";
      }
      span.textContent = line + "\n";
      pre.appendChild(span);
    });
    wrap.appendChild(pre);
    return wrap;
  }

  /**
   * 【查看改动】—— 打开 Diff 预览。
   *
   * @param {Object} alert 告警对象。
   * @param {HTMLElement} button 触发按钮。
   */
  function viewAlertDiff(alert, button) {
    var alertId = String((alert && alert.alert_id) || "");
    if (!alertId) {
      showToast("这条告警没有 ID，无法查看改动。", true);
      return;
    }
    if (!state.activeSession) {
      showToast("当前没有会话，无法查看改动。", true);
      return;
    }
    var url =
      "/api/sessions/" + encodeURIComponent(state.activeSession) +
      "/alerts/" + encodeURIComponent(alertId) + "/diff";
    if (button) {
      button.disabled = true;
    }
    fetch(url)
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "算差异失败", true);
          return;
        }
        openDiffModal(body);
      })
      .catch(function (err) {
        showToast("算差异失败：" + err.message, true);
      })
      .then(function () {
        if (button) {
          button.disabled = false;
        }
      });
  }

  /**
   * 打开 Diff 预览模态框。
   *
   * @param {Object} body 后端返回的差异字典。
   */
  function openDiffModal(body) {
    var content = document.createElement("div");
    content.className = "diff";
    var summary = document.createElement("div");
    summary.className = "diff__summary";
    var point = body.point || {};
    summary.textContent =
      "对比安全点 " + String(point.label || point.id || "（基线）") +
      " → 当前工作目录：" +
      "修改 " + Number(body.modified_count || 0) + " 个、" +
      "新增 " + Number(body.added_count || 0) + " 个、" +
      "删除 " + Number(body.removed_count || 0) + " 个";
    content.appendChild(summary);

    var files = body.files || [];
    if (!files.length) {
      var empty = document.createElement("p");
      empty.className = "diff__empty";
      empty.textContent =
        "没有可比对的内容差异" +
        "（可能是二进制文件，或者两边内容一样）。";
      content.appendChild(empty);
    }
    files.forEach(function (detail) {
      content.appendChild(renderDiffFile(detail));
    });

    var added = body.added || [];
    var removed = body.removed || [];
    if (added.length || removed.length) {
      var list = document.createElement("div");
      list.className = "diff__list";
      added.forEach(function (rel) {
        var item = document.createElement("div");
        item.className = "diff__item diff__item--add";
        item.textContent = "+ " + rel;
        list.appendChild(item);
      });
      removed.forEach(function (rel) {
        var item = document.createElement("div");
        item.className = "diff__item diff__item--del";
        item.textContent = "- " + rel;
        list.appendChild(item);
      });
      content.appendChild(list);
    }

    openModal("Agent 到底改了什么", content, [
      { label: "关闭", kind: "" },
    ]);
  }

  /**
   * 在告警卡片上加【查看改动】按钮。
   *
   * 只有**真的打过安全点**的告警才有 —— 没有快照就没法比。
   *
   * @param {Object} alert 告警对象。
   * @return {HTMLElement|null} 按钮元素；没有安全点时返回 ``null``。
   */
  function buildViewDiffButton(alert) {
    var safePoint = (alert && alert.safe_point) || null;
    if (!safePoint || !safePoint.id) {
      return null;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className =
      "btn btn--ghost btn--sm alert-card__diff alert-card__aux";
    button.textContent = "查看改动";
    button.title = "看看 Agent 在被拦之前到底改了哪些文件";
    button.addEventListener("click", function () {
      viewAlertDiff(alert, button);
    });
    return button;
  }

  /* ------------------------------------------- 回滚并重试（v2.1.0） */

  /**
   * 【回滚并重试】—— 一键复活被挂起的会话。
   *
   * 这一步会做三件**不可逆**的事：
   * 回滚工作目录、结束挂起的进程、重新拉起 Agent。
   * 所以必须先弹计划给用户看，用户点头才动手。
   *
   * @param {Object} alert 告警对象。
   * @param {HTMLElement} button 触发按钮。
   */
  function resumeFromSafePoint(alert, button) {
    var alertId = String((alert && alert.alert_id) || "");
    if (!alertId) {
      showToast("这条告警没有 ID，无法复活。", true);
      return;
    }
    if (!state.activeSession) {
      showToast("当前没有会话，无法复活。", true);
      return;
    }
    var sessionId = state.activeSession;
    var planUrl =
      "/api/sessions/" + encodeURIComponent(sessionId) +
      "/resume-plan";
    if (button) {
      button.disabled = true;
    }
    fetch(planUrl)
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "算复活计划失败", true);
          return;
        }
        openResumeModal(body.plan || {}, alert, sessionId, alertId);
      })
      .catch(function (err) {
        showToast("算复活计划失败：" + err.message, true);
      })
      .then(function () {
        if (button) {
          button.disabled = false;
        }
      });
  }

  /**
   * 打开「复活计划」确认框。
   *
   * @param {Object} plan 后端算出的计划。
   * @param {Object} alert 告警对象。
   * @param {string} sessionId 会话 ID。
   * @param {string} alertId 告警 ID。
   */
  function openResumeModal(plan, alert, sessionId, alertId) {
    var content = document.createElement("div");
    content.className = "resume";
    var point = plan.point || {};
    var restored = Number(plan.restored_count || 0);
    var deleted = Number(plan.deleted_count || 0);
    var agent = plan.agent || {};

    var title = document.createElement("p");
    title.className = "resume__lead";
    title.textContent =
      "回滚到安全点「" + String(point.label || point.id || "基线") +
      "」，并把纠偏提示交给 Agent。";
    content.appendChild(title);

    var list = document.createElement("ul");
    list.className = "resume__list";
    [
      "恢复 " + restored + " 个被修改的文件",
      "删除 " + deleted + " 个 Agent 新建的文件",
      agent.suspended
        ? "结束那个卡在危险命令上的挂起进程"
        : "Agent 当前没有处于挂起状态",
      "重新拉起 Agent，任务提示里会带上纠偏提示",
    ].forEach(function (text) {
      var item = document.createElement("li");
      item.textContent = text;
      list.appendChild(item);
    });
    content.appendChild(list);

    var warn = document.createElement("p");
    warn.className = "resume__warn";
    // ⚠️ 一行写完 —— 相邻的两个字符串字面量会被 ASI 拆成两条语句，
    // 第二段变成空操作（``node --check`` 查不出来，但文字会少一半）。
    warn.textContent =
      "⚠️ 这三步不可逆：被拦下的那条命令不会再执行，" +
      "但 Agent 在那之后的改动也会一起没了。";
    content.appendChild(warn);

    openModal("回滚并重试", content, [
      { label: "取消", kind: "" },
      {
        label: "确认复活",
        kind: "btn--danger",
        onClick: function () {
          closeModal();
          doResume(sessionId, alertId, alert);
        },
      },
    ]);
  }

  /**
   * 真正执行复活。
   *
   * @param {string} sessionId 会话 ID。
   * @param {string} alertId 告警 ID。
   * @param {Object} alert 告警对象。
   */
  function doResume(sessionId, alertId, alert) {
    var url =
      "/api/sessions/" + encodeURIComponent(sessionId) +
      "/alerts/" + encodeURIComponent(alertId) + "/resume";
    pushConsole("正在回滚并重试 …", "event");
    fetch(url, { method: "POST" })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "复活失败", true);
          pushConsole("复活失败：" + (body.error || "-"), "alert");
          return;
        }
        var message =
          String(body.message || "") ||
          "[SENTINEL] 已回滚至安全点，并注入纠偏提示，重新唤醒 Agent。";
        showToast(message);
        pushConsole(message, "event");
        (body.steps || []).forEach(function (step) {
          pushConsole("  · " + step, "line");
        });
      })
      .catch(function (err) {
        showToast("复活失败：" + err.message, true);
      });
  }

  /**
   * 在告警卡片上加【回滚并重试】按钮。
   *
   * 只给**红线级**且**打过安全点**的告警 ——
   * 那是「Agent 被挂起、可以重来一次」的唯一场合。
   *
   * @param {Object} alert 告警对象。
   * @return {HTMLElement|null} 按钮元素；条件不满足时返回 ``null``。
   */
  function buildResumeButton(alert) {
    if (!isRedLine(alert)) {
      return null;
    }
    var safePoint = (alert && alert.safe_point) || null;
    if (!safePoint || !safePoint.id) {
      return null;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className =
      "btn btn--danger btn--sm alert-card__resume alert-card__aux";
    button.textContent = "回滚并重试";
    button.title =
      "回滚到安全点 + 注入纠偏提示 + 重新拉起 Agent（一键复活）";
    button.addEventListener("click", function () {
      resumeFromSafePoint(alert, button);
    });
    return button;
  }

  /* --------------------------------------------- 案发时间线（v2.2.0） */

  /**
   * 拉取并打开「案发时间线」。
   *
   * 时间线回答的是用户早上最想知道的那个问题：
   * 「在触发拦截的 10 分钟前，Agent 到底在干什么」。
   */
  function openTimeline() {
    if (!state.activeSession) {
      showToast("当前没有会话，无法查看时间线。", true);
      return;
    }
    var url =
      "/api/sessions/" + encodeURIComponent(state.activeSession) + "/timeline";
    fetch(url)
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "生成时间线失败", true);
          return;
        }
        openTimelineModal(body);
      })
      .catch(function (err) {
        showToast("生成时间线失败：" + err.message, true);
      });
  }

  /**
   * 渲染时间线弹窗。
   *
   * @param {Object} body 后端返回的时间线字典。
   */
  function openTimelineModal(body) {
    var content = document.createElement("div");
    content.className = "timeline";

    var nodes = body.nodes || [];
    var summary = body.summary || {};
    if (!nodes.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent =
        "这次会话还没有可复盘的节点 —— 还没开始目标，也没有告警。";
      content.appendChild(empty);
      openModal("案发时间线", content, []);
      return;
    }

    var head = document.createElement("div");
    head.className = "timeline__summary";
    head.textContent =
      "共 " + Number(summary.total || nodes.length) + " 个节点 · " +
      (summary.first_text || "--:--:--") + " → " +
      (summary.last_text || "--:--:--") +
      (summary.span ? " · 跨度 " + summary.span : "");
    content.appendChild(head);

    nodes.forEach(function (node) {
      content.appendChild(buildTimelineNode(node));
    });

    openModal("案发时间线", content, []);
  }

  /**
   * 造一个时间线节点元素。
   *
   * @param {Object} node 后端返回的节点字典。
   * @return {HTMLElement} 节点元素。
   */
  function buildTimelineNode(node) {
    var item = document.createElement("div");
    item.className =
      "timeline__node timeline__node--" + String(node.severity || "info");

    var dot = document.createElement("span");
    dot.className = "timeline__dot";
    item.appendChild(dot);

    var main = document.createElement("div");
    main.className = "timeline__main";

    var head = document.createElement("div");
    head.className = "timeline__head";

    var time = document.createElement("span");
    time.className = "timeline__time mono";
    time.textContent = String(node.time || "--:--:--");
    head.appendChild(time);

    var gap = String((node.extra || {}).gap_text || "");
    if (gap) {
      var gapEl = document.createElement("span");
      gapEl.className = "timeline__gap";
      gapEl.textContent = "距上一步 " + gap;
      head.appendChild(gapEl);
    }

    var kind = document.createElement("span");
    kind.className = "timeline__kind timeline__kind--" + String(node.kind || "");
    kind.textContent = timelineKindLabel(node.kind);
    head.appendChild(kind);

    main.appendChild(head);

    var title = document.createElement("div");
    title.className = "timeline__title";
    title.textContent = String(node.title || "");
    main.appendChild(title);

    var detail = String(node.detail || "").trim();
    if (detail) {
      var detailEl = document.createElement("div");
      detailEl.className = "timeline__detail";
      detailEl.textContent = detail;
      main.appendChild(detailEl);
    }

    // 节点关联到安全点时，给一个「看当时改了什么」的入口。
    if (node.has_diff && node.point_id) {
      var link = document.createElement("button");
      link.type = "button";
      link.className = "btn btn--ghost btn--sm timeline__diff";
      link.textContent = "看当时的改动";
      link.title = "对比安全点与当前工作目录";
      link.addEventListener("click", function () {
        viewTimelineDiff(node.point_id);
      });
      main.appendChild(link);
    }

    item.appendChild(main);
    return item;
  }

  /**
   * 节点类型的中文标签。
   *
   * @param {String} kind 节点类型。
   * @return {String} 中文标签。
   */
  function timelineKindLabel(kind) {
    var labels = {
      goal: "开始目标",
      drift: "意图漂移",
      redline: "红线拦截",
      stuck: "上下文卡死",
      loop: "疑似死循环",
      leak: "敏感数据外泄",
      alert: "告警",
      action: "操作",
      safe_point: "安全点"
    };
    return labels[String(kind || "")] || String(kind || "节点");
  }

  /**
   * 按安全点看文件差异（时间线上的入口）。
   *
   * @param {String} pointId 安全点 ID。
   */
  function viewTimelineDiff(pointId) {
    if (!state.activeSession) {
      showToast("当前没有会话，无法查看改动。", true);
      return;
    }
    var url =
      "/api/sessions/" + encodeURIComponent(state.activeSession) +
      "/diff?point_id=" + encodeURIComponent(pointId || "");
    fetch(url)
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "算差异失败", true);
          return;
        }
        openDiffModal(body);
      })
      .catch(function (err) {
        showToast("算差异失败：" + err.message, true);
      });
  }

  /* --------------------------------------------- 开箱安全体检（v2.2.0） */

  /**
   * 跑一次安全体检并打开报告面板。
   */
  function openHealth() {
    fetch("/api/health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "体检失败", true);
          return;
        }
        openHealthModal(body);
      })
      .catch(function (err) {
        showToast("体检失败：" + err.message, true);
      });
  }

  /**
   * 渲染体检报告面板。
   *
   * @param {Object} body 后端返回的体检结果。
   */
  function openHealthModal(body) {
    var content = document.createElement("div");
    content.className = "health";

    var items = body.items || [];
    var counts = { ok: 0, warn: 0, fail: 0, skip: 0 };
    items.forEach(function (item) {
      var key = String(item.status || "");
      if (counts[key] === undefined) {
        counts[key] = 0;
      }
      counts[key] += 1;
    });

    var summary = document.createElement("div");
    summary.className = "health__summary";
    summary.textContent =
      "正常 " + counts.ok + " · 警告 " + counts.warn +
      " · 失败 " + counts.fail + " · 跳过 " + counts.skip;
    content.appendChild(summary);

    var list = document.createElement("div");
    list.className = "health__list";
    items.forEach(function (item) {
      list.appendChild(buildHealthItem(item));
    });
    content.appendChild(list);

    openModal("安全体检报告", content, [
      {
        label: "重新体检",
        kind: "btn--ghost",
        onClick: function () {
          closeModal();
          openHealth();
        }
      }
    ]);
  }

  /**
   * 造一条体检结果元素。
   *
   * @param {Object} item 后端返回的体检项。
   * @return {HTMLElement} 元素。
   */
  function buildHealthItem(item) {
    var row = document.createElement("div");
    row.className =
      "health__item health__item--" + String(item.status || "skip");

    var mark = document.createElement("span");
    mark.className = "health__mark";
    mark.textContent = healthMark(item.status);
    row.appendChild(mark);

    var main = document.createElement("div");
    main.className = "health__main";

    var name = document.createElement("div");
    name.className = "health__name";
    name.textContent = String(item.name || "");
    main.appendChild(name);

    var detail = document.createElement("div");
    detail.className = "health__detail";
    detail.textContent = String(item.detail || "");
    main.appendChild(detail);

    var hint = String(item.hint || "").trim();
    if (hint) {
      var hintEl = document.createElement("div");
      hintEl.className = "health__hint";
      hintEl.textContent = hint;
      main.appendChild(hintEl);
    }

    row.appendChild(main);
    return row;
  }

  /**
   * 体检状态对应的记号。
   *
   * @param {String} status 状态。
   * @return {String} 记号。
   */
  function healthMark(status) {
    var marks = { ok: "✓", warn: "!", fail: "✗", skip: "–" };
    return marks[String(status || "")] || "?";
  }

  function showToast(message, isError) {
    if (!el.toast) {
      return;
    }
    el.toast.textContent = message;
    el.toast.classList.toggle("toast--error", !!isError);
    el.toast.hidden = false;
    if (toastTimer) {
      window.clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(function () {
      el.toast.hidden = true;
    }, 3600);
  }

  function pushConsole(text, kind) {
    if (!el.consoleLines) {
      return;
    }
    consoleSeq += 1;
    var row = document.createElement("div");
    row.className = "console__line console__line--" + (kind || "event");
    row.textContent = "[" + nowText() + "] " + text;
    el.consoleLines.appendChild(row);
    while (el.consoleLines.childElementCount > MAX_CONSOLE_LINES) {
      el.consoleLines.removeChild(el.consoleLines.firstElementChild);
    }
    if (el.consoleCount) {
      el.consoleCount.textContent = String(consoleSeq);
    }
  }

  /* --------------------------------------------------------- 计数条 */

  function updateCounters() {
    var counts = state.counts || {};
    var dangerous = Number(counts.DANGEROUS || 0);
    var loop = Number(counts.LOOP || 0);
    var total = 0;
    Object.keys(counts).forEach(function (key) {
      total += Number(counts[key] || 0);
    });
    if (el.countDangerous) {
      el.countDangerous.textContent = String(dangerous);
    }
    if (el.countLoop) {
      el.countLoop.textContent = String(loop);
    }
    if (el.countTotal) {
      el.countTotal.textContent = String(total);
    }
    var onTrack = state.scanCounts && state.scanCounts.ON_TRACK;
    if (el.countOnTrack) {
      el.countOnTrack.textContent =
        onTrack === undefined || onTrack === null ? "–" : String(onTrack);
    }
    updateTokenCounter();
    updateCascadeCounter();
  }

  /* -------------------------------------------------- Token 计数（v1.1.0） */

  /* 与后端 src/core/tokens.py 的 format_token_count 保持一致：
   * ≥ 1 万用「万」做单位，否则直接给数字。 */
  function formatTokenCount(tokens) {
    var value = Number(tokens || 0);
    if (!isFinite(value) || value < 0) {
      value = 0;
    }
    if (value >= 10000) {
      return (value / 10000).toFixed(1) + " 万";
    }
    return String(Math.round(value));
  }

  function updateTokenCounter() {
    if (!el.countToken) {
      return;
    }
    var tokens = state.tokens || {};
    var total = Number(tokens.total_tokens || 0);
    el.countToken.textContent = formatTokenCount(total);
    if (el.countTokenBox) {
      /* 估算值必须**标出来** —— 绝不让估算冒充真值。 */
      el.countTokenBox.dataset.estimated = tokens.estimated ? "true" : "false";
      var events = Number(tokens.events || 0);
      var avg = Number(tokens.avg_per_event || 0);
      el.countTokenBox.title =
        "本次会话累计消耗 " + total + " Token" +
        (tokens.estimated ? "（含估算值）" : "（真实 usage）") +
        " · 已处理 " + events + " 个事件 · 每步均摊 " +
        (Math.round(avg * 10) / 10) + " Token";
    }
  }

  function updateStateChip(status) {
    var key = String(status || "idle");
    // v3.5.1：Status Pulse 呼吸灯跟着状态走 ——
    // 运行 = 绿色跳动；暂停 / 停止 = 灰色常亮。
    if (el.statusPulse) {
      el.statusPulse.dataset.state = key;
    }
    if (!el.runState) {
      return;
    }
    el.runState.textContent = key.toUpperCase();
    el.runState.dataset.state = key;
  }

  /* ------------------------------------- 级联审核计数器（v3.5.0） */

  /**
   * 刷新顶部那行「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」。
   *
   * ⚠️ **数字必须是真的**：
   *    L2 判正常而被静默丢弃的次数，与因此省下的云端调用次数，
   *    是**同一个数**（每丢一次就省一次）——
   *    这里如实写出来，绝不拿别的数字凑数。
   */
  function updateCascadeCounter() {
    if (!el.countCascadeText) {
      return;
    }
    var cascade = state.cascade || {};
    var saved = Number(cascade.saved_cloud_calls || 0);
    var l2Calls = Number(cascade.l2_calls || 0);
    var l3Calls = Number(cascade.l3_calls || 0);
    var unavailable = Number(cascade.unavailable_calls || 0);
    var redLights = Number(cascade.red_lights || 0);
    el.countCascadeText.textContent =
      "L2 已过滤 " + saved + " 次冗余，为您节省 " + saved + " 次云端调用";
    if (el.countCascadeBox) {
      var detail =
        "级联审核（v3.5.0）：L2 本地模型累计判定 " + l2Calls + " 次，其中 " +
        saved + " 次判「正常」被静默丢弃 —— 这 " + saved +
        " 次没有升级到 L3 云端模型。另有 " + l3Calls +
        " 次升级到 L3 复审，" + redLights + " 次双盲亮红灯";
      if (unavailable > 0) {
        detail += "，" + unavailable + " 次 L2 不可用（已回退 L1 原始判定）";
      }
      el.countCascadeBox.title = detail + "。";
      el.countCascadeBox.dataset.active = l2Calls > 0 ? "true" : "false";
    }
  }

  /* --------------------------------------------------------- 告警卡 */

  function buildAlertCard(alert) {
    var card = document.createElement("article");
    card.className =
      "alert-card alert-card--" + severityClass(alert.verdict);
    // v2.4.0：把判定写进 data 属性 —— 截图 / 自动化脚本能**精确定位**
    // 到某一种告警（比如 DRIFT），不必靠猜样式类名。
    card.dataset.verdict = String(alert.verdict || "UNKNOWN");
    if (isRedLine(alert)) {
      card.classList.add("alert-card--redline");
    }

    var head = document.createElement("div");
    head.className = "alert-card__head";

    var verdict = document.createElement("span");
    verdict.className = "verdict";
    verdict.textContent = String(alert.verdict || "UNKNOWN");
    head.appendChild(verdict);

    var rule = document.createElement("span");
    rule.className = "rule-id";
    rule.textContent = String(alert.rule_id || "-");
    head.appendChild(rule);

    if (isRedLine(alert)) {
      var redline = document.createElement("span");
      redline.className = "tag tag--redline";
      redline.textContent = "红线级";
      head.appendChild(redline);
    }

    if (String(alert.verdict || "").toUpperCase() === "DRIFT") {
      var driftTag = document.createElement("span");
      driftTag.className = "tag tag--drift";
      driftTag.textContent = "意图漂移";
      head.appendChild(driftTag);
    }

    if (alert.tool && alert.tool !== "-") {
      var tool = document.createElement("span");
      tool.className = "tag tag--tool";
      tool.textContent = String(alert.tool);
      head.appendChild(tool);
    }

    var time = document.createElement("time");
    time.className = "alert-card__time";
    time.textContent = alertTime(alert);
    head.appendChild(time);
    card.appendChild(head);

    var reason = document.createElement("p");
    reason.className = "alert-card__reason";
    reason.textContent = String(alert.reason || "（无规则原因）");
    card.appendChild(reason);

    var command = String(alert.command || "");
    var cmd = document.createElement("pre");
    cmd.className = "alert-card__cmd";
    if (command) {
      cmd.textContent = command;
    } else {
      cmd.className = "alert-card__cmd alert-card__cmd--empty";
      cmd.textContent = "（本次事件没有命令文本）";
    }
    card.appendChild(cmd);

    var explain = document.createElement("div");
    explain.className = "explain";
    var text = document.createElement("p");
    text.className = "explain__text";
    if (alert.explanation) {
      text.textContent = String(alert.explanation);
    } else if (alert.explanation_pending) {
      // ⚠️ L2 解释是**异步**的 —— 卡片先出现，解释稍后淡进来。
      // 这里必须区分「还在生成」与「压根没有」，
      // 否则用户会以为这个事件永远不会有解释了。
      text.textContent = "模型解释生成中…";
      text.classList.add("explain__text--pending");
      explain.classList.add("explain--pending");
    } else {
      text.textContent = "本次事件没有附带解释。";
    }
    if (alert.explanation_degraded) {
      text.classList.add("explain__text--degraded");
    }
    explain.appendChild(text);

    if (alert.explanation_degraded) {
      var tag = document.createElement("span");
      tag.className = "tag tag--degraded";
      tag.textContent = "规则原始判定（模型未提供解释）";
      explain.appendChild(tag);
    }
    card.appendChild(explain);

    // Token 挽回提示（v1.1.0）——
    // 「本次异常已拦截，预估为您挽回 X 万 Token 的无效消耗」。
    var savedText = String(alert.token_saved_text || "");
    if (savedText) {
      var saved = document.createElement("div");
      saved.className = "token-saved";
      var savedIcon = document.createElement("span");
      savedIcon.className = "token-saved__icon";
      savedIcon.textContent = "💰";
      saved.appendChild(savedIcon);
      var savedLabel = document.createElement("span");
      savedLabel.className = "token-saved__text";
      savedLabel.textContent = "本次异常已拦截，" + savedText;
      saved.appendChild(savedLabel);
      var detail = alert.token_saved || {};
      saved.title =
        "已消耗 " + Number(detail.burned || 0) + " Token（这段工作正在走向危险动作）" +
        " + 预估还会消耗 " + Number(detail.upcoming || 0) +
        " Token（按每步 " + Number(detail.avg_per_event || 0) +
        " Token × " + Number(detail.steps || 0) + " 步估算）";
      card.appendChild(saved);
    }

    // 操作区：干预按钮（仅 DANGEROUS）+ 复制纠偏提示（每张卡片都有）。
    card.appendChild(buildAlertActions(alert));
    if (alert.alert_id) {
      // 追加更新（L2 解释回来）靠它对上号。
      card.dataset.alertId = String(alert.alert_id);
    }
    return card;
  }

  /* -------------------------------------------- 追加更新（解释淡入） */

  function applyAlertUpdate(update) {
    if (!update || !update.alert_id) {
      return false;
    }
    var card = null;
    if (el.alertList) {
      card = el.alertList.querySelector(
        '[data-alert-id="' + String(update.alert_id) + '"]'
      );
    }
    if (!card) {
      // 原告警不在列表里（比如页面刚打开、或已被挤掉）——
      // 这时**不新开卡片**：宁可少显示，也不能凭空多一张。
      return false;
    }
    var explain = card.querySelector(".explain");
    if (!explain) {
      return false;
    }
    var text = explain.querySelector(".explain__text");
    if (!text) {
      return false;
    }
    var value = String(update.explanation || "");
    if (!value) {
      value = "本次事件没有附带解释。";
    }
    text.textContent = value;
    text.classList.remove("explain__text--pending");
    text.classList.add("explain__text--updated");
    explain.classList.remove("explain--pending");
    explain.classList.add("explain--updated");
    if (update.explanation_degraded) {
      text.classList.add("explain__text--degraded");
      if (!explain.querySelector(".tag--degraded")) {
        var tag = document.createElement("span");
        tag.className = "tag tag--degraded";
        tag.textContent = "规则原始判定（模型未提供解释）";
        explain.appendChild(tag);
      }
    }
    return true;
  }

  function prependAlert(alert) {
    if (!el.alertList) {
      return;
    }
    if (el.alertEmpty && el.alertEmpty.parentNode === el.alertList) {
      el.alertList.removeChild(el.alertEmpty);
    }
    var card = buildAlertCard(alert);
    el.alertList.insertBefore(card, el.alertList.firstChild);
    while (el.alertList.childElementCount > 60) {
      el.alertList.removeChild(el.alertList.lastElementChild);
    }
    // 卡片是新的，按钮的可用状态要跟着当前守护者状态重新刷一遍。
    refreshGuardianButtons();
  }

  /* --------------------------------------------------------- 日志行 */

  /**
   * 追加**一批**日志行（v2.4.0）。
   *
   * 一秒几百行时，逐行 appendChild 会让浏览器做几百次重排 ——
   * 这里用一个 DocumentFragment **一次性**插进去。
   *
   * @param {Array<string>} texts 日志行文本数组。
   */
  function appendLogLines(texts) {
    if (!el.logLines || !texts || !texts.length) {
      return;
    }
    if (el.logEmpty && el.logEmpty.parentNode === el.logLines) {
      el.logLines.removeChild(el.logEmpty);
    }
    var base = state.logRawBase + state.logRaw.length;
    var firstIndex = state.logRaw.length;
    for (var i = 0; i < texts.length; i += 1) {
      state.logRaw.push(String(texts[i] == null ? "" : texts[i]));
    }
    if (logScaleMode(state.logCompression) === "extreme") {
      /* 极限摘要（v3.7.0）：合并是**全局相邻**关系 ——
       *   新来的这一行可能并进上一组的尾巴，也可能把上一组**打断**。
       *   逐行 append 永远合不起来，所以这一档只能**整块重画**。
       *   （极限档本来就是「只留几行」的档位，重画成本可以接受。） */
      redrawLogLines();
    } else {
      var fragment = document.createDocumentFragment();
      for (var k = 0; k < texts.length; k += 1) {
        // v3.6.0：按**当前档位**造这一行；自然语言模式下
        //   噪声行 / 工具结果行直接不造（返回 null）。
        var row = buildLogRow(base + k, state.logRaw[firstIndex + k]);
        if (row) {
          fragment.appendChild(row);
        }
      }
      el.logLines.appendChild(fragment);
    }
    trimLogLines();
    // ⚠️ v3.5.1：**只有**用户还在底部时才自动滚到底。
    //   用户往上翻历史时，哨兵**绝不**再把他拽回最底下 ——
    //   想回最新，点右下角【↓ 回到最新】。
    if (!state.logFollow) {
      return;
    }
    var box = logScrollBox();
    if (box) {
      box.scrollTop = box.scrollHeight;
    }
  }

  /**
   * 把超出上限的日志行**从缓存与 DOM 里一起砍掉**（v3.6.0）。
   *
   * ⚠️ 两边必须同步 —— 否则重画时又会把早就砍掉的行画回来。
   *
   * @return {void}
   */
  function trimLogLines() {
    var dropped = 0;
    while (state.logRaw.length > MAX_LOG_LINES) {
      state.logRaw.shift();
      dropped += 1;
    }
    if (dropped) {
      state.logRawBase += dropped;
      var rows = el.logLines.querySelectorAll(".log-line");
      for (var i = 0; i < rows.length; i += 1) {
        var index = Number(rows[i].getAttribute("data-raw-index"));
        if (index < state.logRawBase) {
          if (rows[i].parentNode) {
            rows[i].parentNode.removeChild(rows[i]);
          }
        }
      }
    }
    while (el.logLines.childElementCount > MAX_LOG_LINES) {
      el.logLines.removeChild(el.logLines.firstElementChild);
    }
  }

  function appendLogLine(text) {
    if (!el.logLines) {
      return;
    }
    appendLogLines([text]);
  }

  function clearLogLines() {
    if (!el.logLines) {
      return;
    }
    while (el.logLines.firstElementChild) {
      el.logLines.removeChild(el.logLines.firstElementChild);
    }
    // v3.6.0：缓存也要一起清 —— 否则重画会把旧行带回来。
    state.logRaw = [];
    state.logRawBase = 0;
  }

  /* ----------------------------------------------------------- 状态 */

  function applyState(data) {
    if (!data || typeof data !== "object") {
      return;
    }
    state.counts = data.counts || {};
    state.scanCounts = data.scan_counts || {};
    state.tokens = data.tokens || {};
    // v3.5.0：级联审核统计 + 线程指纹分路。
    state.cascade = data.cascade || {};
    state.streams = data.streams || [];
    state.paused = !!data.paused;
    updateCounters();
    updateStateChip(data.status);

    var lines = data.recent_lines || [];
    if (lines.length && !logContentUnchanged(lines)) {
      // v3.5.1：重画时保住用户的阅读位置（脱离跟随时）。
      replaceLogLines(lines);
    }
    var alerts = data.alerts || [];
    renderAlerts(alerts);
    // 实时审核流水（v3.3.1）——
    // 切换会话 / 刷新页面时靠它把流水补回来，
    // 不然用户看到的是一块**空白**面板。
    if (data.ontrack) {
      renderLiveAudit(data.ontrack);
    }
    if (el.pauseButton) {
      el.pauseButton.textContent = state.paused ? "恢复推送" : "暂停推送";
    }
    refreshGuardianButtons();
  }

  function loadState() {
    // 有活动会话时走会话级接口 —— 面板显示的必须是**当前选中那一路**。
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) + "/state"
      : "/api/state";
    return fetch(url)
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (state.activeSession) {
          // 会话级 state 不带告警列表 —— 告警由 /alerts 单独拉。
          data.alerts = [];
          applyState(data);
        } else {
          applyState(data);
        }
      })
      .catch(function (err) {
        pushConsole("拉取状态失败：" + err.message, "line");
      });
  }

  /* ----------------------------------------------------------- 扫描 */

  function triggerScan() {
    if (!el.scanButton) {
      return;
    }
    el.scanButton.disabled = true;
    el.scanButton.textContent = "扫描中…";
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) + "/scan"
      : "/api/scan";
    fetch(url, { method: "POST" })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        if (result.ok && result.body && result.body.ok) {
          showToast("扫描完成，新告警将实时推送");
          pushConsole("扫描完成", "event");
        } else {
          var message =
            (result.body && result.body.error) || "扫描失败";
          showToast(message, true);
          pushConsole(message, "alert");
        }
      })
      .catch(function (err) {
        showToast("扫描失败：" + err.message, true);
      })
      .then(function () {
        el.scanButton.disabled = false;
        el.scanButton.textContent = "立即扫描";
      });
  }

  /* ------------------------------------------------------ 生成晨报 */

  /**
   * 生成并保存《哨兵值守晨报》（v1.2.0）。
   *
   * 晨报会落到「晨报归档/晨报-YYYY-MM-DD.md」，
   * 同时把摘要推到看板上。
   */
  function triggerBriefing() {
    if (!el.briefingButton) {
      return;
    }
    el.briefingButton.disabled = true;
    el.briefingButton.textContent = "生成中…";
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) + "/briefing"
      : "/api/briefing";
    fetch(url, { method: "POST" })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        var body = result.body || {};
        if (!result.ok || !body.ok) {
          showToast(body.error || "生成晨报失败", true);
          pushConsole("生成晨报失败：" + (body.error || "-"), "alert");
          return;
        }
        var path = String(body.path || "");
        showToast(path ? "晨报已保存：" + path : "晨报已生成");
        pushConsole("晨报已生成" + (path ? "：" + path : ""), "event");
        if (body.terminal) {
          pushConsole(String(body.terminal), "line");
        }
      })
      .catch(function (err) {
        showToast("生成晨报失败：" + err.message, true);
      })
      .then(function () {
        el.briefingButton.disabled = false;
        el.briefingButton.textContent = "生成晨报";
      });
  }

  function togglePause() {    var next = !state.paused;
    var url = state.activeSession
      ? "/api/sessions/" + encodeURIComponent(state.activeSession) + "/pause"
      : "/api/pause";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused: next })
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        state.paused = !!data.paused;
        updateStateChip(data.status);
        if (el.pauseButton) {
          el.pauseButton.textContent = state.paused ? "恢复推送" : "暂停推送";
        }
        showToast(state.paused ? "已暂停实时推送" : "已恢复实时推送");
        loadState();
      })
      .catch(function (err) {
        showToast("切换失败：" + err.message, true);
      });
  }

  /* --------------------------------------------------------- 事件流 */

  function handleEvent(event) {
    if (!event || !event.type) {
      return;
    }
    // v3.0.0：正在切页 → **不处理**任何事件。
    //
    // 路由器换 body 的时候，旧页面的 SSE 可能还有最后几条事件在飞 ——
    // 这时候去改 DOM，改的是**已经被换掉**的节点，
    // 白费力气，还可能抛错。直接丢掉。
    if (window.__SENTINEL_ROUTING__) {
      return;
    }
    // 多路会话下每条事件都带 session_id ——
    // 只有当前会话的事件才进可见的列表，别的会话只更新计数。
    var sessionId = String(event.session_id || "");
    var mine = isActiveSession(sessionId);

    if (event.type === "alert") {
      bumpSessionCount(sessionId, event.verdict);
      if (mine) {
        // v3.5.0：按虚拟会话筛选 —— 别的那一路的告警不进来。
        if (matchesStream(event)) {
          prependAlert(event);
          // v3.5.1：告警也要在流水区留一句人话 ——
          // 以前「出事的那一刻」在流水区**根本看不见**。
          appendAuditAlert(event);
        }
        state.counts = state.counts || {};
        state.counts[event.verdict] =
          (state.counts[event.verdict] || 0) + 1;
        updateCounters();
      }
      renderSessions();
      pushConsole(
        "告警 " + event.verdict + " · " + (event.rule_id || "-") +
          (sessionId ? " [" + sessionId + "]" : ""),
        "alert"
      );
      return;
    }
    if (event.type === "alert_update") {
      // L2 解释异步回来了 —— 淡进**已有**那张卡片，不新开一张。
      var applied = mine ? applyAlertUpdate(event) : false;
      pushConsole(
        applied
          ? "解释补充 · " + (event.alert_id || "-")
          : "解释补充（未找到对应卡片）· " + (event.alert_id || "-"),
        applied ? "event" : "alert"
      );
      return;
    }
    if (event.type === "line") {
      if (mine) {
        appendLogLine(String(event.text || ""));
      }
      return;
    }
    if (event.type === "lines") {
      // v2.4.0：**批量**日志行 —— 一秒几百行时逐条发会把前端
      // 和订阅队列一起冲垮，所以服务端攒成一批发。
      if (mine) {
        appendLogLines(event.texts || []);
      }
      return;
    }
    if (event.type === "on_track") {
      // 实时审核流水（v3.3.1）——
      // L1 判定为 ON_TRACK 的一行极简摘要。
      // 它**不是**告警：只往流水区加一行灰字，不进告警列表。
      if (mine) {
        appendLiveAudit(event);
      }
      return;
    }
    if (event.type === "cascade") {
      // v3.5.0：级联审核统计更新 ——
      // 顶部那行「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」靠它刷新。
      state.cascade = event;
      updateCascadeCounter();
      pushConsole(
        "级联 · L2 " + (event.l2_calls || 0) + " 次 / L3 " +
        (event.l3_calls || 0) + " 次 / 省下 " +
        (event.saved_cloud_calls || 0) + " 次云端调用",
        "event"
      );
      return;
    }
    if (event.type === "timeline_event") {
      // 操作事件（v2.2.0）—— 开始目标 / 复活重试 / 热重载……
      // 它**不是**告警，所以只往控制台里记一笔，不进告警列表。
      pushConsole(
        "操作 " + (event.title || event.kind || "-") +
          (sessionId ? " [" + sessionId + "]" : ""),
        "event"
      );
      return;
    }
    if (event.type === "tokens") {
      // Token 账单行（v1.1.0）—— 只更新当前会话的计数器。
      if (mine) {
        state.tokens = event;
        updateTokenCounter();
      }
      return;
    }
    if (event.type === "status") {
      if (mine) {
        if (event.counts) {
          state.counts = event.counts;
          updateCounters();
        }
        updateStateChip(event.status);
      }
      return;
    }
    if (event.type === "session_state") {
      // 有会话结束了 —— 刷新会话列表，让状态条跟上。
      loadSessions();
      return;
    }
    if (event.type === "guardian") {
      pushConsole(
        "守护者 " + (event.action || "-") + " · " + (event.rule_id || "-") +
          (event.session_id ? " [" + event.session_id + "]" : ""),
        event.ok === false ? "alert" : "event"
      );
      loadSessionGuardian(state.activeSession);
      return;
    }
    if (event.type === "arm") {
      // v2.4.0：武装状态变了 —— 刷新会话列表与守护者状态。
      pushConsole(
        "守护者武装 · " + (event.session_id || "-") + " → " +
          (event.armed ? "已武装" : "已解除"),
        "event"
      );
      loadSessions();
      loadSessionGuardian(state.activeSession);
      return;
    }
    if (event.type === "task_goal") {
      updateGoalUi(event.goal || "");
      // 目标来源可能变了（人工指定），面板要跟着刷新。
      loadSessionTarget(state.activeSession);
      pushConsole(
        "任务目标已更新：" + (event.goal || "（已清空，漂移检测待命）"),
        "event"
      );
    }
  }

  function connect() {
    if (typeof EventSource === "undefined") {
      pushConsole("浏览器不支持 EventSource，无法实时推送", "alert");
      return;
    }
    var source = new EventSource("/api/events");
    // v2.3.0：记下来 —— 路由切走时要把这条连接收掉。
    eventSource = source;
    source.onmessage = function (message) {
      var payload = null;
      try {
        payload = JSON.parse(message.data);
      } catch (err) {
        // ⚠️ 解析失败要**留痕** —— 静默丢掉的话，
        // 「看板不更新了」这种问题根本查不出原因（v3.0.0）。
        console.error("事件流数据解析失败：", err, message.data);
        return;
      }
      handleEvent(payload);
    };
    source.onerror = function () {
      pushConsole("事件流断开，浏览器会自动重连", "line");
    };
  }

  /**
   * 收摊（v2.3.0）—— 路由切走时把后台的东西都停掉。
   */
  function destroy() {
    if (eventSource) {
      try {
        eventSource.close();
      } catch (err) {
        // ⚠️ 不许悄悄吞掉 —— 前端没有 logger，
        // 浏览器控制台就是它的日志（v3.0.0）。
        console.error("关闭事件流失败：", err);
      }
      eventSource = null;
    }
    // v3.5.1：摘掉日志面板的滚动监听。
    if (logScrollNode) {
      logScrollNode.removeEventListener("scroll", onLogScroll);
      logScrollNode = null;
    }
    // v3.6.0：摘掉窗口尺寸监听与待执行的重画帧。
    if (typeof window.removeEventListener === "function") {
      window.removeEventListener("resize", onWindowResize);
    }
    // v3.7.0：档位滑条连同它的重画帧一起**删干净了** ——
    //   档位改由服务端下发，不再有「本地滑条 → 重画帧」这条路。
    timers.forEach(function (id) {
      window.clearInterval(id);
    });
    timers = [];
    if (toastTimer) {
      window.clearTimeout(toastTimer);
      toastTimer = null;
    }
    closeModal();
  }

  /* ----------------------------------------------------------- 启动 */

  /**
   * 「退出演示，开始真实扫描」（v3.0.1）——
   *
   * ⚠️ 为什么要做这个
   * ================
   *
   * v3.0.0 的实测反馈：「双击 exe 直接进了演示模式，
   * 导致我以为雷达扫不到真实的进程。」
   *
   * 用户看到一屏**模拟数据**，会以为「这就是雷达的全部结果」——
   * 于是得出「雷达扫不到东西」的错误结论。
   * 演示模式反而**挡住了**用户。
   *
   * 所以这个按钮做三件事：
   * 1. 让服务端**真的**停掉演示进程（终端窗口随之关闭）；
   * 2. 在页面上留下**能直接抄**的真命令；
   * 3. 明确告诉他「刚才看到的全是假的」。
   *
   * ⚠️ 第 2 步必须在第 1 步**之前**渲染完 ——
   * 服务端一停，页面就再也拿不到任何数据了。
   */
  function exitDemoMode() {
    if (!el.demoExitButton || el.demoExitButton.disabled) {
      return;
    }
    el.demoExitButton.disabled = true;
    el.demoExitButton.textContent = "正在退出演示……";

    fetch("/api/demo/exit", {
      method: "POST",
      headers: { "Content-Type": "application/json" }
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) {
          var reason = (data && data.error) || "未知错误";
          el.demoExitButton.disabled = false;
          el.demoExitButton.textContent = "退出演示，开始真实扫描 →";
          showToast("退出演示失败：" + reason, true);
          return;
        }
        var lines = (data && data.instructions) || [];
        if (el.demoExitCode) {
          el.demoExitCode.textContent = lines.join("\n");
        }
        if (el.demoExitPanel) {
          el.demoExitPanel.hidden = false;
        }
        // 服务端马上要停了 —— 先把 SSE 关掉，免得控制台刷一屏网络错误。
        if (source) {
          try {
            source.close();
          } catch (err) {
            console.error("关闭事件流失败：", err);
          }
          source = null;
        }
      })
      .catch(function (err) {
        el.demoExitButton.disabled = false;
        el.demoExitButton.textContent = "退出演示，开始真实扫描 →";
        showToast("退出演示失败：" + err.message, true);
      });
  }

  function toggleConsole() {
    var open = el.consoleToggle.getAttribute("aria-expanded") === "true";
    el.consoleToggle.setAttribute("aria-expanded", open ? "false" : "true");
    el.consoleBody.hidden = open;
  }

  /* ------------------------------------------------- 关闭哨兵（v3.4.0） */
  //
  // 用户实测：「找不到退出系统的按钮，关掉黑框后后台变成
  // 无法杀死的幽灵（只能去任务管理器杀）。」
  //
  // 网页**不能**自己关掉自己，但它**可以**让后台那个进程
  // 把自己收干净 —— 托盘的【彻底退出】走的是同一条路。
  //
  // ⚠️ 一定要先确认：误点一下就把监控停掉了。

  /**
   * 弹出「确认关闭」的对话框。
   */
  function confirmShutdown() {
    var content = document.createElement("div");
    content.className = "shutdown";
    var title = document.createElement("p");
    title.textContent =
      "确定要彻底关闭哨兵吗？";
    content.appendChild(title);

    var list = document.createElement("ul");
    [
      "看板服务会停下来，托盘图标会一起消失",
      "哨兵自己拉起的 Agent 子进程会被结束",
      "雷达挂载进来的 Agent **不受影响** —— 那是你的进程，哨兵不会去动它",
    ].forEach(function (text) {
      var item = document.createElement("li");
      item.textContent = text;
      list.appendChild(item);
    });
    content.appendChild(list);

    openModal("关闭哨兵", content, [
      { label: "取消", kind: "" },
      {
        label: "确认关闭",
        kind: "btn--danger",
        onClick: function () {
          closeModal();
          doShutdown();
        },
      },
    ]);
  }

  /**
   * 真的去关哨兵。
   */
  function doShutdown() {
    if (el.shutdownButton) {
      el.shutdownButton.disabled = true;
      el.shutdownButton.textContent = "正在关闭…";
    }
    // 服务端马上要停了 —— 先把 SSE 关掉，免得控制台刷一屏网络错误。
    if (source) {
      try {
        source.close();
      } catch (err) {
        console.error("关闭事件流失败：", err);
      }
      source = null;
    }
    fetch("/api/shutdown", { method: "POST" })
      .then(function () {
        showToast("哨兵正在退出，托盘图标马上会消失。");
      })
      .catch(function (err) {
        if (el.shutdownButton) {
          el.shutdownButton.disabled = false;
          el.shutdownButton.textContent = "关闭哨兵";
        }
        showToast("关闭哨兵失败：" + err.message, true);
      });
  }

  /**
   * 卸载一个会话（v3.4.0）。
   *
   * 用户实测：「UI 无法删除选错的/失效的挂载会话。」
   *
   * 点【×】之后**立刻**从列表里消失 ——
   * 那个会话已经失效了，让它留在列表里占位只会让人更烦。
   *
   * @param {string} sessionId 会话 ID。
   */
  function detachSession(sessionId) {
    var id = String(sessionId || "");
    if (!id) {
      return;
    }
    fetch("/api/sessions/" + encodeURIComponent(id), {
      method: "DELETE"
    })
      .then(function (res) {
        return res.json().catch(function () {
          return {};
        });
      })
      .then(function (data) {
        if (data && data.ok === false) {
          showToast(
            "卸载失败：" + (data.error || "未知错误"),
            true
          );
          return;
        }
        // 立刻从本地列表里去掉 —— 不等下一次轮询。
        state.sessions = (state.sessions || []).filter(function (item) {
          return String(item.id) !== id;
        });
        if (state.activeSession === id) {
          state.activeSession = "";
        }
        renderSessionList();
        showToast("已卸载该会话，哨兵不再跟随它。");
      })
      .catch(function (err) {
        showToast("卸载失败：" + err.message, true);
      });
  }

  function init() {
    if (el.demoExitButton) {
      el.demoExitButton.addEventListener("click", exitDemoMode);
    }
    if (el.shutdownButton) {
      el.shutdownButton.addEventListener("click", confirmShutdown);
    }
    if (el.scanButton) {
      el.scanButton.addEventListener("click", triggerScan);
    }
    if (el.pauseButton) {
      el.pauseButton.addEventListener("click", togglePause);
    }
    // v3.5.0：点一下「只看这一路」按钮 —— 恢复显示全部。
    if (el.streamFilter) {
      el.streamFilter.addEventListener("click", function () {
        clearStreamFilter();
        pushConsole("取消虚拟会话筛选，显示全部路", "event");
      });
    }
    if (el.briefingButton) {
      el.briefingButton.addEventListener("click", triggerBriefing);
    }
    if (el.timelineButton) {
      el.timelineButton.addEventListener("click", openTimeline);
    }
    if (el.healthButton) {
      el.healthButton.addEventListener("click", openHealth);
    }
    if (el.attachButton) {
      el.attachButton.addEventListener("click", function () {
        openAttachModal(false);
      });
    }
    // v3.5.1：日志跟随锁定 ——
    // 监听滚动：用户往上翻就**立刻**解除跟随，往回底部就恢复。
    logScrollNode = logScrollBox();
    if (logScrollNode) {
      logScrollNode.addEventListener("scroll", onLogScroll);
      state.logFollow = logAtBottom();
      updateLogFollowUi();
    }
    if (el.logFollowButton) {
      el.logFollowButton.addEventListener("click", scrollLogToLatest);
    }
    // v3.7.0：呈现档位来自**服务端**（``/api/presentation``）——
    //   设置页改一次，所有看板窗口一起变。
    //   ⚠️ 主看板**不再有**档位滑条了（v3.6.0 的那个已整条移除）。
    loadPresentation();
    // v3.6.0：多面板拖拽与折叠 —— 拖出来的比例会存进 localStorage。
    initLayout();
    // v3.9.0：移动端沉浸式 Tab 栏 —— 桌面端不显示，但状态照样维护。
    initMobileTabs();
    // v3.3.2：点提示框 → 直接进「手动填路径」那一步。
    if (el.radarAlert) {
      el.radarAlert.addEventListener("click", function () {
        openAttachModal(true);
      });
      el.radarAlert.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openAttachModal(true);
        }
      });
    }
    if (el.consoleToggle) {
      el.consoleToggle.addEventListener("click", toggleConsole);
    }
    if (el.goalForm) {
      el.goalForm.addEventListener("submit", function (event) {
        event.preventDefault();
        submitTaskGoal();
      });
    }
    // v3.3.1 强引导：橘黄高亮的「未设置」与旁边的气泡都把光标
    // 送到顶部的目标输入框 —— 用户的下一步只有一个动作。
    if (el.watchGoalNudge) {
      // 文案以 JS 常量为准 —— 模板里的那份只是「没跑 JS 时也能看见」。
      el.watchGoalNudge.textContent = GOAL_NUDGE_TEXT;
      el.watchGoalNudge.addEventListener("click", focusGoalInput);
    }
    if (el.watchGoalSource) {
      el.watchGoalSource.addEventListener("click", focusGoalInput);
      el.watchGoalSource.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          focusGoalInput();
        }
      });
    }
    // 先把会话列表拉下来 —— 知道有哪些会话了，才知道该显示谁。
    loadSessions().then(function (active) {
      if (active) {
        loadSessionContent(active);
        loadSessionTarget(active);
        loadSessionGuardian(active);
        loadTaskGoal();
      } else {
        loadState();
        loadTaskGoal();
        loadWatchTarget();
      }
    });
    loadGuardianStatus();
    loadProcessActivity();
    connect();
    timers.push(window.setInterval(loadState, 15000));
    timers.push(window.setInterval(loadGuardianStatus, 10000));
    timers.push(window.setInterval(loadSessions, 5000));
    timers.push(window.setInterval(loadProcessActivity, 10000));
    // v3.7.0：轮询呈现档位 —— 设置页改了以后，这个窗口也跟着变。
    timers.push(window.setInterval(loadPresentation, 10000));
  }

  /* ----------------------------------------------------------- 路由契约 */
  //
  // 前端路由（v2.3.0）会**整页替换** body —— 页面必须暴露
  // 「装」（init）与「拆」（destroy）两个口子。
  // 不拆的话，离开看板以后后台还在轮询、还在收 SSE。
  window.SentinelPage = {
    init: init,
    destroy: destroy,
    /* v3.5.1：把「拼人话」的**纯函数**露出来 ——
     *
     * 它们没有副作用（不碰 DOM、不发请求），
     * 露出来才能被自动化测试**真的跑一遍**，
     * 而不是只靠「文件里有没有这个字符串」这种弱断言。
     *
     * ⚠️ 这不是给用户用的接口 —— 只是给测试与排查用的。 */
    audit: {
      sentence: auditSentence,
      alertSentence: auditSentenceFromAlert,
      clip: clipText,
      verb: auditVerb,
      /* v3.7.0：审核流水的压缩档位 —— 同样是纯函数，
       * 这样「100 档到底藏了什么、合并了什么」能被自动化测试跑一遍。 */
      mode: auditCompressionMode,
      visible: auditEntryVisible,
      plain: auditEntryIsPlain,
      /* v3.8.0：系统提示（带 ``audit_text``）的判定 ——
       * 自动化测试要能直接断言「这条提示在极限档也看得见」。 */
      notice: auditEntryIsNotice,
      countText: auditCountText
    },
    /* v3.5.1：雷达空态引导的文案与命令 —— 同样是无副作用的纯函数。 */
    radar: {
      command: radarEmptyCommand,
      /* v3.6.0：显式传入「文件在不在」「路径是什么」——
       * 这样测试不用去改内部状态就能验两种分支。 */
      commandFor: radarEmptyCommand,
      title: RADAR_EMPTY_TITLE,
      noteReady: RADAR_EMPTY_NOTE_READY,
      noteMissing: RADAR_EMPTY_NOTE_MISSING,
      relativePath: RADAR_EMPTY_RELATIVE_PATH
    },
    /* v3.6.0：日志渲染档位的**纯函数** ——
     * 跟 v3.5.1 一样露出来，自动化测试才能真的跑一遍，
     * 而不是只靠「文件里有没有这个字符串」这种弱断言。 */
    log: {
      parse: parseLogEntry,
      mode: logScaleMode,
      label: logScaleLabel,
      tag: logKindTag,
      visible: logRowVisible,
      body: logRowBody,
      stripNoise: stripNoiseSections,
      jsonTokens: logJsonTokens,
      /* v3.7.0：极限摘要的四个纯函数 ——
       * 动作分类 / 目标提取 / 连续合并 / 合并后的那句人话。 */
      action: logActionKind,
      target: logToolTarget,
      merge: mergeLogEntries,
      actionText: logActionText,
      /* v3.9.0：极限摘要的两个原语 ——
       * 与后端 live_audit.py 的同名函数必须同算法，
       * 一致性由 tests/test_v390_release.py 逐条断言。 */
      stripCodeBlocks: stripLogCodeBlocks,
      firstSentences: firstSentences,
      extremeSentences: LOG_EXTREME_SENTENCES
    },
    /* v3.6.0：流水区「核心上下文」的判定 —— 也是纯函数。 */
    flow: {
      entryKind: auditEntryKind
    },
    /* v3.9.0：移动端 Tab —— 白名单与切换逻辑都露出来，
     * 自动化测试可以真的点一遍、真的断言 body 上的属性。 */
    mobile: {
      tabs: MOBILE_TABS,
      defaultTab: MOBILE_TAB_DEFAULT,
      read: readMobileTab,
      apply: applyMobileTab
    }
  };

  // 路由切换时由 router.js 统一调用 init() ——
  // 这里不要**再**自动跑一遍，否则会初始化两遍。
  if (!window.__SENTINEL_ROUTING__) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }
})();
