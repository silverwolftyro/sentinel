/* v3.7.0 前端逻辑的**可执行**测试夹具。
 *
 * 为什么要有这个
 * ==============
 *
 * v3.7.0 的核心是「极限摘要算法」——
 * 连续同类动作要**真的合并**成一行
 * （连续 5 次读文件 → ``[读取] core/auth.py 等 5 个文件``）。
 *
 * ⚠️ 光靠「文件里有没有这个字符串」这种弱断言等于没测：
 *    把合并函数体改成 ``return entries`` 也能照样通过。
 *
 * 所以这里跟 v3.5.1 / v3.6.0 一样用 Node 的 ``vm`` 模块造一个**极简 DOM**，
 * 把 dashboard.js 真的**跑起来**，
 * 再调用它露出来的 ``window.SentinelPage.log`` /
 * ``window.SentinelPage.audit``。
 *
 * ⚠️ ``window.__SENTINEL_ROUTING__ = true`` 是关键：
 *    它让脚本**不要**自动跑 init()（否则会去 fetch 网络）。
 *
 * ⚠️ 这里验的是**纯函数**的行为 ——
 *    真正的 DOM 渲染效果由演示脚本截图取证。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const SOURCE = path.join(ROOT, "src", "gui", "static", "dashboard.js");

/**
 * 造一个极简的 DOM 替身。
 *
 * @returns {Object} 假 document。
 */
function makeDocument() {
  const noop = () => {};
  return {
    readyState: "complete",
    getElementById: () => null,
    createElement: () => ({
      style: {},
      dataset: {},
      classList: { add: noop, remove: noop, contains: () => false },
      appendChild: noop,
      removeChild: noop,
      insertBefore: noop,
      setAttribute: noop,
      getAttribute: () => null,
      addEventListener: noop,
      querySelectorAll: () => [],
      firstElementChild: null,
      textContent: "",
      hidden: false
    }),
    createTextNode: (text) => ({ textContent: text }),
    createDocumentFragment: () => ({ appendChild: noop }),
    addEventListener: noop,
    body: { appendChild: noop, removeChild: noop },
    execCommand: () => true
  };
}

/**
 * 在沙箱里把 dashboard.js 跑起来。
 *
 * @returns {Object} 沙箱里的 window（带 SentinelPage）。
 */
function loadDashboard() {
  const source = fs.readFileSync(SOURCE, "utf8");
  const document = makeDocument();
  const sandbox = {
    window: { __SENTINEL_ROUTING__: true },
    document: document,
    navigator: {},
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    isFinite: isFinite,
    Math: Math,
    Date: Date,
    JSON: JSON,
    String: String,
    Number: Number,
    Object: Object,
    Array: Array,
    Error: Error,
    Promise: Promise,
    fetch: () => Promise.reject(new Error("测试沙箱里没有网络")),
    EventSource: undefined
  };
  sandbox.window.document = document;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "dashboard.js" });
  return sandbox.window;
}

const window = loadDashboard();
const log = window.SentinelPage.log;
const audit = window.SentinelPage.audit;

if (!log || !audit) {
  throw new Error("dashboard.js 没有暴露 window.SentinelPage.log / audit");
}

/* --- 用例 ---------------------------------------------------------- */

const cases = [];

function add(name, got, want) {
  cases.push({ name: name, got: got, want: want });
}

/**
 * 造一条工具调用日志行。
 *
 * @param {string} name 工具名。
 * @param {object} args 工具参数。
 * @returns {string} 一行 JSON。
 */
function toolLine(name, args) {
  return JSON.stringify({
    type: "tool_call",
    tool: { name: name, arguments: JSON.stringify(args) }
  });
}

/**
 * 把若干行日志解析成条目数组（合并算法的输入）。
 *
 * @param {Array<string>} raws 原始行。
 * @returns {Array<object>} 条目数组。
 */
function toEntries(raws) {
  return raws.map((raw, index) => {
    const parsed = log.parse(raw);
    return {
      index: index,
      raw: raw,
      kind: parsed.kind,
      text: parsed.text,
      tool: parsed.tool,
      action: parsed.action,
      target: parsed.target
    };
  });
}

/* --- 一、日志档位多出「极限摘要」这一段 ----------------------------- */

add("99 档 = 高度摘要", log.mode(99), "summary");
add("100 档 = 极限摘要", log.mode(100), "extreme");
add("100 档标签", log.label(100), "极限摘要");
add("99 档标签", log.label(99), "高度摘要");

/* --- 二、动作分类（读 / 写 / 跑）---------------------------------- */

add("Read = 读取", log.action("Read"), "read");
add("read_file = 读取", log.action("read_file"), "read");
add("Glob = 读取", log.action("Glob"), "read");
add("TodoRead = 读取", log.action("TodoRead"), "read");
add("Write = 写入", log.action("Write"), "write");
add("Edit = 写入", log.action("Edit"), "write");
/* ⚠️ 写入类必须**先判** ——
 *    ``search_replace_content`` 里有「search」，
 *    先判读取会被误归成「读取」。 */
add("search_replace_content = 写入", log.action("search_replace_content"), "write");
add("Bash = 执行", log.action("Bash"), "exec");
add("shell = 执行", log.action("shell"), "exec");
add("run_command = 执行", log.action("run_command"), "exec");
add("认不出来的工具 = 其它", log.action("mystery_tool"), "other");
add("空工具名 = 其它", log.action(""), "other");

/* --- 三、目标提取 --------------------------------------------------- */

const readEntry = log.parse(toolLine("Read", { file_path: "core/auth.py" }));
add("读操作的目标是路径", readEntry.target, "core/auth.py");
add("读操作的动作类别", readEntry.action, "read");

const writeEntry = log.parse(
  toolLine("Write", { file_path: "core/auth.py", content: "x" })
);
add("写操作的目标是路径", writeEntry.target, "core/auth.py");

const execEntry = log.parse(toolLine("Bash", { command: "pytest -q" }));
add("执行操作的目标是命令", execEntry.target, "pytest -q");

/* 字符串化的 arguments 也要能挖出来。 */
const stringArgsEntry = log.parse(
  JSON.stringify({
    type: "tool_call",
    tool: { name: "Read", arguments: '{"file_path": "core/users.py"}' }
  })
);
add("字符串化 arguments 也能挖出目标", stringArgsEntry.target, "core/users.py");

/* ⚠️ 看板拿到的是**原始日志行** ——
 *    mcode 的旧协议把工具调用放在 ``tool_call`` 里，
 *    v0.6 协议放在 ``toolCall`` 里。这两种形态也必须挖得出
 *    工具名与目标，否则真实日志里的极限摘要全是「未命名工具」。 */
const mcodeOldEntry = log.parse(
  JSON.stringify({
    type: "tool_call",
    tool_call: { name: "Read", arguments: { file_path: "core/auth.py" } }
  })
);
add("mcode 旧协议也挖得出工具名", mcodeOldEntry.tool, "Read");
add("mcode 旧协议也挖得出目标", mcodeOldEntry.target, "core/auth.py");

const mcodeV6Entry = log.parse(
  JSON.stringify({
    type: "item.updated",
    item: {
      type: "tool_call",
      toolCall: { name: "Read", input: { file_path: "core/users.py" } }
    }
  })
);
add("mcode v0.6 协议也挖得出工具名", mcodeV6Entry.tool, "Read");
add("mcode v0.6 协议也挖得出目标", mcodeV6Entry.target, "core/users.py");

const claudeEntry = log.parse(
  JSON.stringify({
    type: "assistant",
    message: {
      content: [{ type: "tool_use", name: "Read", input: { file_path: "core/x.py" } }]
    }
  })
);
add("Claude 风格也挖得出工具名", claudeEntry.tool, "Read");
add("Claude 风格也挖得出目标", claudeEntry.target, "core/x.py");

/* --- 四、连续同类动作**真合并** ------------------------------------ */

// 用户要的例子：连续 5 次读文件 → 一行。
const fiveReads = toEntries([
  toolLine("Read", { file_path: "core/auth.py" }),
  toolLine("Read", { file_path: "core/users.py" }),
  toolLine("Read", { file_path: "core/roles.py" }),
  toolLine("Read", { file_path: "core/deps.py" }),
  toolLine("Read", { file_path: "core/utils.py" })
]);
const mergedReads = log.merge(fiveReads);

add("连续 5 次读合并成 1 行", mergedReads.length, 1);
add("合并行的动作类别", mergedReads[0].action, "read");
add("合并行的计数", mergedReads[0].count, 5);
add("合并行保留首个目标", mergedReads[0].target, "core/auth.py");
add(
  "合并行的人话就是用户要的那一句",
  log.actionText(mergedReads[0]),
  "[读取] core/auth.py 等 5 个文件"
);

/* 中间夹了别的动作 ⇒ **不算**连续。 */
const mixed = toEntries([
  toolLine("Read", { file_path: "a.py" }),
  toolLine("Read", { file_path: "b.py" }),
  toolLine("Write", { file_path: "c.py", content: "x" }),
  toolLine("Read", { file_path: "d.py" }),
  toolLine("Read", { file_path: "e.py" })
]);
const mergedMixed = log.merge(mixed);
add("夹了别的动作就分成 3 段", mergedMixed.length, 3);
add("第一段合并 2 条", mergedMixed[0].count, 2);
add("中间那一条单独成行", mergedMixed[1].kind, "tool");
add("最后一段合并 2 条", mergedMixed[2].count, 2);

/* 只有一条时不合并 —— 免得平白少掉一行信息。 */
const single = toEntries([toolLine("Read", { file_path: "only.py" })]);
const mergedSingle = log.merge(single);
add("只有一条时不合并", mergedSingle.length, 1);
add("只有一条时仍是普通行", mergedSingle[0].kind, "tool");

/* 命令的单位是「条命令」，文件的单位是「个文件」。 */
const threeCommands = toEntries([
  toolLine("Bash", { command: "npm test" }),
  toolLine("Bash", { command: "npm run lint" }),
  toolLine("Bash", { command: "npm run build" })
]);
const mergedCommands = log.merge(threeCommands);
add(
  "连续 3 条命令的单位是「条命令」",
  log.actionText(mergedCommands[0]),
  "[执行] npm test 等 3 条命令"
);

/* 认不出目标的条目**不合并** —— 合并了更糊涂。 */
const noTarget = toEntries([
  toolLine("Mystery", { foo: "bar" }),
  toolLine("Mystery", { foo: "baz" })
]);
add("认不出目标的不合并", log.merge(noTarget).length, 2);

/* --- 五、审核流水的压缩档位 ---------------------------------------- */

add("0 档 = 完整", audit.mode(0), "full");
add("39 档 = 完整", audit.mode(39), "full");
add("40 档 = 精简", audit.mode(40), "compact");
add("69 档 = 精简", audit.mode(69), "compact");
add("70 档 = 结论", audit.mode(70), "conclusion");
add("89 档 = 结论", audit.mode(89), "conclusion");
add("90 档 = 极限", audit.mode(90), "extreme");
add("100 档 = 极限", audit.mode(100), "extreme");
add("非法档位退回默认（60 = 精简）", audit.mode("abc"), "compact");

const thinkingEntry = { event_type: "thinking", snippet: "想法" };
const toolEntry = { event_type: "tool_call", summary: "读取" };
const userEntry = { event_type: "user_message", snippet: "问题" };
const replyEntry = { event_type: "text", snippet: "回答" };
const resultEntry = { event_type: "result" };
const alertEntry = { type: "alert", stream_kind: "alert", severity: "high" };

add("完整档显示思考", audit.visible(thinkingEntry, "full"), true);
add("精简档藏起思考", audit.visible(thinkingEntry, "compact"), false);
add("结论档藏起思考", audit.visible(thinkingEntry, "conclusion"), false);
add("极限档藏起思考", audit.visible(thinkingEntry, "extreme"), false);

add("结论档藏起普通工具调用", audit.visible(toolEntry, "conclusion"), false);
add("精简档保留普通工具调用", audit.visible(toolEntry, "compact"), true);

add("结论档保留用户提问", audit.visible(userEntry, "conclusion"), true);
add("结论档保留 Agent 回答", audit.visible(replyEntry, "conclusion"), true);
add("结论档保留本轮结论", audit.visible(resultEntry, "conclusion"), true);
add("极限档保留告警", audit.visible(alertEntry, "extreme"), true);
add("极限档保留用户提问", audit.visible(userEntry, "extreme"), true);

add("普通流水 = 工具调用", audit.plain(toolEntry), true);
add("告警不算普通流水", audit.plain(alertEntry), false);
add("用户提问不算普通流水", audit.plain(userEntry), false);

/* --- 六、流水计数行 ------------------------------------------------- */

add(
  "计数行说清条数与时间",
  audit.countText({ count: 5, first: "10:00:01", last: "10:00:09" }),
  "⚡ 极限摘要：正常流水 5 条（至 10:00:09）"
);

/* --- 七、SSE 新行也必须走合并（结构守卫） --------------------------- */

/*
 * ⚠️ 这是一条**结构守卫**，不是行为测试 —— 说清楚免得误以为它更强。
 *
 * 合并是「**全局相邻**」关系：新推来的一行可能并进上一组的尾巴，
 * 也可能把上一组**打断**。所以极限档下 SSE 每来一批，
 * 必须**整块重画**（``redrawLogLines``），
 * 逐行 append 永远合不起来 —— 这个坑实机演示时真踩过。
 *
 * 极简 DOM 替身跑不了这条链路（要真的 EventSource + 真实元素），
 * 所以这里守住**源码结构**，防止有人把那个分支删掉。
 */
const appendSource = (function () {
  const src = fs.readFileSync(SOURCE, "utf8");
  const start = src.indexOf("function appendLogLines");
  const end = src.indexOf("\n  }", start);
  return start >= 0 && end > start ? src.slice(start, end) : "";
})();

add(
  "SSE 推新行时，极限档必须整块重画（才会走合并）",
  appendSource.indexOf('"extreme"') >= 0 &&
    appendSource.indexOf("redrawLogLines()") >= 0,
  true
);

/* --- 八、汇总 ------------------------------------------------------- */

let failed = 0;
cases.forEach((item) => {
  const got = JSON.stringify(item.got);
  const want = JSON.stringify(item.want);
  if (got !== want) {
    failed += 1;
    process.stdout.write(
      "FAIL " + item.name + "\n  期望 " + want + "\n  实际 " + got + "\n"
    );
  } else {
    process.stdout.write("ok   " + item.name + "\n");
  }
});

process.stdout.write(
  "v3.7.0 前端逻辑用例：" + (cases.length - failed) + "/" + cases.length + " 通过\n"
);
if (failed) {
  process.exitCode = 1;
}
