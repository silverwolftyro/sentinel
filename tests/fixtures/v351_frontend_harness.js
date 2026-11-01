/* v3.5.1 前端逻辑的**可执行**测试夹具。
 *
 * 为什么要有这个
 * ==============
 *
 * 看板前端是一个浏览器里的 IIFE —— 纯函数的断言如果只写成
 * 「文件里有没有这个字符串」，那测的是**文本**，不是**行为**：
 * 把函数体改成 return null 也能照样通过。
 *
 * 所以这里用 Node 的 ``vm`` 模块造一个**极简 DOM**，
 * 把 dashboard.js 真的**跑起来**，
 * 再调用它露出来的 ``window.SentinelPage.audit``。
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
      addEventListener: noop,
      querySelectorAll: () => [],
      firstElementChild: null,
      textContent: "",
      hidden: false
    }),
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
const audit = window.SentinelPage.audit;

if (!audit) {
  throw new Error("dashboard.js 没有暴露 window.SentinelPage.audit");
}

/* --- 用例 ---------------------------------------------------------- */

const cases = [];

cases.push({
  name: "thinking 说人话",
  got: audit.sentence({
    event_type: "thinking",
    snippet: "关键发现是鉴权中间件没有校验 refresh token",
    summary: "正常思考"
  }),
  want: {
    text: "Agent 正在思考：关键发现是鉴权中间件没有校验 refresh token",
    level: "ok"
  }
});

cases.push({
  name: "tool_call 说人话",
  got: audit.sentence({
    event_type: "tool_call",
    snippet: "pytest -q tests/",
    summary: "正常执行: pytest"
  }),
  want: { text: "正常执行命令：pytest -q tests/", level: "ok" }
});

cases.push({
  name: "读文件按后端动词说话",
  got: audit.sentence({
    event_type: "tool_call",
    snippet: "core/engine.py",
    summary: "正常读取: core/engine.py"
  }),
  want: { text: "正常读取：core/engine.py", level: "ok" }
});

cases.push({
  name: "摘要在 40 字处截断",
  got: audit.sentence({
    event_type: "thinking",
    snippet: "一".repeat(80),
    summary: "正常思考"
  }),
  want: { text: "Agent 正在思考：" + "一".repeat(39) + "…", level: "ok" }
});

cases.push({
  name: "高危命令 = 红色",
  got: audit.alertSentence({
    verdict: "DANGEROUS",
    severity: "standard",
    level: "critical",
    command: "rm -rf /",
    reason: "匹配到高危命令: rm -rf /"
  }),
  want: { text: "危险！拦截到高危命令：rm -rf /", level: "danger" }
});

cases.push({
  name: "死循环 = 橘黄",
  got: audit.alertSentence({
    verdict: "LOOP",
    severity: "standard",
    level: "warn",
    reason: "连续 3 次读取同一个文件，疑似死循环: auth.py"
  }),
  want: {
    text: "警告：连续 3 次读取同一个文件，疑似死循环: auth.py",
    level: "warn"
  }
});

cases.push({
  name: "红线级 = 红色",
  got: audit.alertSentence({
    verdict: "DANGEROUS",
    severity: "red_line",
    level: "critical",
    command: "rm -rf C:/",
    reason: "红线级高危命令"
  }),
  want: { text: "危险！拦截到高危命令：rm -rf C:/", level: "danger" }
});

cases.push({
  name: "没有命令就退回理由",
  got: audit.alertSentence({
    verdict: "DANGEROUS",
    severity: "standard",
    level: "critical",
    command: "",
    reason: "匹配到高危命令: chmod 777 /etc"
  }),
  want: { text: "危险！拦截到高危命令：chmod 777 /etc", level: "danger" }
});

cases.push({
  name: "快照里的告警也走人话告警",
  got: audit.sentence({
    stream_kind: "alert",
    verdict: "DANGEROUS",
    severity: "red_line",
    level: "critical",
    command: "rm -rf /",
    reason: "红线级命中：递归删除根目录 / 家目录 / 整盘（/）"
  }),
  want: { text: "危险！拦截到高危命令：rm -rf /", level: "danger" }
});

cases.push({
  name: "SSE 推来的告警也走人话告警",
  got: audit.sentence({
    type: "alert",
    verdict: "LOOP",
    severity: "standard",
    level: "warn",
    reason: "连续 3 次读取同一个文件，疑似死循环: auth.py"
  }),
  want: {
    text: "警告：连续 3 次读取同一个文件，疑似死循环: auth.py",
    level: "warn"
  }
});

cases.push({
  name: "动词解析",
  got: audit.verb("正常读取: auth.py"),
  want: "读取"
});

cases.push({
  name: "压长句",
  got: audit.clip("一 二\t三\n四", 3),
  want: "一…"
});

cases.push({
  name: "空态引导文案逐字一致",
  got: window.SentinelPage.radar.title,
  want: "未发现正在运行的 Agent。若要开始监控，请在终端中运行："
});

cases.push({
  name: "空态引导命令逐字一致（文件已存在时用绝对路径）",
  // v3.6.0：空态命令现在**按文件在不在**分流 ——
  // 文件已存在才用绝对路径，不存在时只给相对路径。
  // 这里显式传入「已存在」，验的还是 v3.5.1 那条老契约。
  got: window.SentinelPage.radar.commandFor(
    true,
    "D:\\项目\\<项目目录>\\logs\\capture.jsonl"
  ),
  want: 'mcode exec "你的指令" > D:\\项目\\<项目目录>\\logs\\capture.jsonl'
});

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
  "v3.5.1 前端逻辑用例：" + (cases.length - failed) + "/" + cases.length + " 通过\n"
);
if (failed) {
  process.exitCode = 1;
}
