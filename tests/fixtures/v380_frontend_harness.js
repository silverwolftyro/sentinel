/* v3.8.0 前端解析逻辑的**可执行**测试夹具。
 *
 * 为什么要有这个
 * ==============
 *
 * v3.8.0 的核心是「解析器盲区补齐」——
 * 两件事必须被**真的跑一遍**才算数：
 *
 * 1. Claude 风格的**同一行**里 ``thinking`` 在前 + ``tool_use`` 在后时，
 *    工具调用不能被思考块顶掉（v3.7.0 的遗留盲区）；
 * 2. 看板解析出来的工具名，必须与**后端引擎适配器**解析出来的**完全一致**
 *    —— 否则就会出现「日志里有工具名，看板解析不出来」。
 *
 * ⚠️ 第 2 条靠 ``--dump`` 模式与 Python 侧的 pytest 夹具**对拍**完成：
 *    本脚本把每行原始日志解析成 ``{kind, tool, action, target}``，
 *    Python 侧拿适配器的输出跟它逐行比对。
 *
 * 跟 v3.5.1 / v3.6.0 / v3.7.0 一样，这里用 Node 的 ``vm`` 模块造一个
 * **极简 DOM**，把 dashboard.js 真的**跑起来**，
 * 再调用它露出来的 ``window.SentinelPage.log``。
 *
 * ⚠️ ``window.__SENTINEL_ROUTING__ = true`` 是关键：
 *    它让脚本**不要**自动跑 init()（否则会去 fetch 网络）。
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

const win = loadDashboard();
const log = win.SentinelPage.log;
const audit = win.SentinelPage.audit;

if (!log || !audit) {
  throw new Error("dashboard.js 没有暴露 window.SentinelPage.log / audit");
}

/* ------------------------------------------------------------------ */
/* --dump 模式：给 Python 侧的一致性夹具对拍用                          */
/* ------------------------------------------------------------------ */

/*
 * ⚠️ 分发必须放在断言**之前** ——
 *    dump 模式只往 stdout 打 JSON，不能夹带别的输出。
 *    ``dumpMode`` 是函数声明（会提升），所以这里先调用没问题。
 */
const dumpIndex = process.argv.indexOf("--dump");
if (dumpIndex >= 0) {
  const target = process.argv[dumpIndex + 1];
  if (!target) {
    throw new Error("--dump 后面要跟 JSONL 文件路径");
  }
  dumpMode(target);
  process.exit(0);
}

/**
 * 解析一批原始日志行，输出机器可读的结果。
 *
 * ⚠️ 这个模式**只往 stdout 打 JSON** ——
 *    Python 侧要直接把它读成结构，不能夹带别的输出。
 *
 * @param {string} filePath JSONL 文件路径。
 * @returns {void}
 */
function dumpMode(filePath) {
  const raw = fs.readFileSync(filePath, "utf8");
  const lines = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  const rows = lines.map((line) => {
    const parsed = log.parse(line);
    return {
      raw: line,
      kind: parsed.kind,
      tool: parsed.tool,
      action: parsed.action,
      target: parsed.target
    };
  });
  process.stdout.write(JSON.stringify(rows));
}

/* ------------------------------------------------------------------ */
/* 断言模式                                                             */
/* ------------------------------------------------------------------ */

const cases = [];

function add(name, got, want) {
  cases.push({ name: name, got: got, want: want });
}

/* --- 一、Claude 同行 thinking + tool_use（v3.8.0 核心盲区）--------- */

/*
 * ⚠️ 这是 v3.7.0 留下的**真盲区**：
 *    外层 ``message`` 节点自带 ``role:"assistant"``，
 *    而 ``thinking`` 块又排在 ``tool_use`` 块前面 ——
 *    前序遍历「首个命中即返回」会先命中思考块，
 *    工具名与目标**全丢**（日志里有工具名，看板却解析不出来）。
 */
const claudeSameLine = log.parse(
  JSON.stringify({
    type: "assistant",
    message: {
      role: "assistant",
      content: [
        { type: "thinking", thinking: "先读一下配置" },
        {
          type: "tool_use",
          id: "toolu_v380_1",
          name: "Read",
          input: { file_path: "core/auth.py" }
        }
      ]
    }
  })
);
add("同行 thinking+tool_use：类别是工具", claudeSameLine.kind, "tool");
add("同行 thinking+tool_use：工具名不丢", claudeSameLine.tool, "Read");
add("同行 thinking+tool_use：目标不丢", claudeSameLine.target, "core/auth.py");
add("同行 thinking+tool_use：动作类别", claudeSameLine.action, "read");

/* 外层带 role:"assistant" 也要一样 —— 真实 Claude Code 就是这个形态。 */
const claudeSameLineWithRole = log.parse(
  JSON.stringify({
    type: "assistant",
    message: {
      id: "msg_v380",
      type: "message",
      role: "assistant",
      model: "claude-sonnet-4",
      content: [
        { type: "thinking", thinking: "跑个测试" },
        {
          type: "tool_use",
          id: "toolu_v380_2",
          name: "Bash",
          input: { command: "pytest -q" }
        }
      ]
    },
    parent_tool_use_id: null,
    session_id: "sess_v380"
  })
);
add("完整 Claude 事件：工具名", claudeSameLineWithRole.tool, "Bash");
add("完整 Claude 事件：目标是命令", claudeSameLineWithRole.target, "pytest -q");
add("完整 Claude 事件：动作是执行", claudeSameLineWithRole.action, "exec");

/* thinking 在**后**面时也一样认得（顺序反过来也要对）。 */
const claudeToolFirst = log.parse(
  JSON.stringify({
    type: "assistant",
    message: {
      role: "assistant",
      content: [
        { type: "tool_use", name: "Edit", input: { file_path: "core/deps.py" } },
        { type: "thinking", thinking: "改完了" }
      ]
    }
  })
);
add("tool_use 在前 thinking 在后：工具名", claudeToolFirst.tool, "Edit");

/* --- 二、工具回执**不能**被误判成工具调用 -------------------------- */

/*
 * ⚠️ ``tool_result`` 是「工具的回执」，不是「工具调用」。
 *    它归类为 ``tool_result``，**不是** ``tool`` ——
 *    所以工具优先的遍历不能把它误当成工具调用。
 */
const claudeToolResult = log.parse(
  JSON.stringify({
    type: "user",
    message: {
      role: "user",
      content: [
        { type: "tool_result", tool_use_id: "toolu_v380_1", content: "ok" }
      ]
    }
  })
);
add("tool_result 不是工具调用", claudeToolResult.kind, "user");
add("tool_result 没有工具名", claudeToolResult.tool, "");

/* --- 三、既有的各类形态**不能**被改坏 ------------------------------ */

const plainUser = log.parse(
  JSON.stringify({ type: "user_message", content: "帮我看看认证模块" })
);
add("用户消息仍然是用户", plainUser.kind, "user");

const plainReply = log.parse(
  JSON.stringify({ type: "text", text: "好的，我来看看" })
);
add("Agent 回答仍然是回答", plainReply.kind, "reply");

const plainThinking = log.parse(
  JSON.stringify({ type: "thinking", thinking: "让我先看看配置文件……" })
);
add("思考仍然是思考", plainThinking.kind, "think");
add("思考正文也要挖得到", plainThinking.text, "让我先看看配置文件……");

/* ⚠️ ``thinking`` 这个键名后端认（``_parse_block``），
 *    前端此前不认 —— 那一行只显示「其他」，思考正文整个丢。
 *    v3.8.0 把两边对齐。
 *
 * ⚠️ 类别是「Agent 回答」而不是「思考」——
 *    命中的是**外层** assistant 消息节点（它自带 ``role:"assistant"``）。
 *    真正要修的是**正文丢失**（此前正文是空的，只剩一个「其他」标签）。 */
const claudeThinkingOnly = log.parse(
  JSON.stringify({
    type: "assistant",
    message: {
      role: "assistant",
      content: [{ type: "thinking", thinking: "先确认一下目录结构" }]
    }
  })
);
add("只有思考块的 Claude 行不再退化成「其他」", claudeThinkingOnly.kind, "reply");
add("只有思考块的 Claude 行有正文", claudeThinkingOnly.text, "先确认一下目录结构");

const mcodeOld = log.parse(
  JSON.stringify({
    type: "tool_call",
    tool_call: { name: "Read", arguments: { file_path: "core/auth.py" } }
  })
);
add("mcode 旧协议：工具名", mcodeOld.tool, "Read");
add("mcode 旧协议：目标", mcodeOld.target, "core/auth.py");

const mcodeToolNest = log.parse(
  JSON.stringify({
    type: "tool_call",
    tool: { name: "Write", arguments: { file_path: "core/users.py" } }
  })
);
add("mcode tool 嵌套：工具名", mcodeToolNest.tool, "Write");

const mcodeV6 = log.parse(
  JSON.stringify({
    type: "item.completed",
    item: {
      type: "tool_call",
      toolCall: { id: "call_v6_read_1", name: "Read", input: { file_path: "core/roles.py" } }
    }
  })
);
add("mcode v0.6 协议：工具名", mcodeV6.tool, "Read");
add("mcode v0.6 协议：目标", mcodeV6.target, "core/roles.py");

/* --- 四、与后端适配器对齐的三个形态（v3.8.0 补齐）------------------ */

/* 后端适配器认 ``tool-call``（带连字符），前端此前不认。 */
const dashToolCall = log.parse(
  JSON.stringify({
    type: "tool-call",
    tool_call: { name: "Grep", arguments: { pattern: "TODO" } }
  })
);
add("tool-call（连字符）也是工具", dashToolCall.kind, "tool");
add("tool-call（连字符）：工具名", dashToolCall.tool, "Grep");

/* 后端适配器认 ``toolName`` 嵌套。 */
const toolNameNest = log.parse(
  JSON.stringify({
    type: "tool_call",
    toolName: { name: "Glob", input: { pattern: "*.py" } }
  })
);
add("toolName 嵌套：工具名", toolNameNest.tool, "Glob");

/* 后端适配器认 ``functionCall`` 嵌套。 */
const functionCallNest = log.parse(
  JSON.stringify({
    type: "tool_call",
    functionCall: { name: "Shell", arguments: { command: "dir" } }
  })
);
add("functionCall 嵌套：工具名", functionCallNest.tool, "Shell");

/* --- 五、共享夹具必须能整份解析 ------------------------------------- */

/* ⚠️ 这份 JSONL 同时是 Python 侧一致性夹具的输入 ——
 *    前后端对拍时用的就是同一批行。 */
const sharedFixture = path.join(__dirname, "v380_parse_consistency.jsonl");
const sharedLines = fs
  .readFileSync(sharedFixture, "utf8")
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter((line) => line.length > 0);
add("共享夹具的行数", sharedLines.length, 13);

/* 每一行都必须能被解析（不许抛异常、不许解析成空）。 */
let unparsed = 0;
sharedLines.forEach((line) => {
  const parsed = log.parse(line);
  if (!parsed || !parsed.kind) {
    unparsed += 1;
  }
});
add("共享夹具每一行都解析得动", unparsed, 0);

/* --- 六、系统提示在**任何**档位都要看得见（v3.8.0）------------------ */

/*
 * ⚠️ 这是 L2 掉线热切 L3 时那条提示 ——
 *    「结论」「极限」档本来只留核心上下文与告警，
 *    要是把系统提示也藏了，用户就完全不知道
 *    「刚才那一轮其实走了云端」——
 *    而这恰恰是最需要知道的事。
 */
const failoverNotice = {
  event_type: "cascade_notice",
  audit_text: "[WARN] L2 掉线，已自动切换至 L3 深度审查",
  audit_level: "warn",
  summary: "L2 掉线，已自动切换至 L3 深度审查"
};

add("系统提示算系统提示", audit.notice(failoverNotice), true);
add("完整档看得见系统提示", audit.visible(failoverNotice, "full"), true);
add("精简档看得见系统提示", audit.visible(failoverNotice, "compact"), true);
add("结论档看得见系统提示", audit.visible(failoverNotice, "conclusion"), true);
add("极限档看得见系统提示", audit.visible(failoverNotice, "extreme"), true);

const noticeSentence = audit.sentence(failoverNotice);
add("系统提示就显示那句话", noticeSentence.text, failoverNotice.audit_text);
add("系统提示是橘黄档", noticeSentence.level, "warn");

/* ⚠️ 没有 ``audit_text`` 的普通流水**不受影响** ——
 *    极限档照旧把它合并成计数行。 */
const plainToolCall = { event_type: "tool_call", summary: "读取" };
add("普通工具调用不算系统提示", audit.notice(plainToolCall), false);
add("极限档仍然藏起普通工具调用", audit.visible(plainToolCall, "extreme"), false);
add("极限档仍然把普通工具调用算进合并", audit.plain(plainToolCall), true);

/* 告警有自己的通道，不算系统提示，但本来就条条可见。 */
const budgetAlert = { type: "alert", stream_kind: "alert", severity: "high" };
add("告警不算系统提示", audit.notice(budgetAlert), false);
add("告警在极限档仍然看得见", audit.visible(budgetAlert, "extreme"), true);

/* --- 七、汇总 ------------------------------------------------------- */

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
  "v3.8.0 前端解析用例：" + (cases.length - failed) + "/" + cases.length + " 通过\n"
);
if (failed) {
  process.exitCode = 1;
}
