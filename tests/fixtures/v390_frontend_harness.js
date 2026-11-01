/* v3.9.0 前端逻辑的**可执行**测试夹具。
 *
 * 覆盖两件必须真的跑一遍才算数的事：
 *
 * 1. **极限摘要的剥离** —— 100 档里代码块要彻底不见，
 *    长文本只留前两句；
 * 2. **移动端 Tab** —— 四个 Tab 的白名单、切换、以及
 *    body 上的 ``data-mobile-tab`` 真的被改写。
 *
 * 跟 v3.5.1 / v3.6.0 / v3.7.0 / v3.8.0 一样，这里用 Node 的
 * ``vm`` 模块造一个**极简 DOM**，把 dashboard.js 真的**跑起来**，
 * 再调用它露出来的 ``window.SentinelPage``。
 *
 * ⚠️ ``window.__SENTINEL_ROUTING__ = true`` 是关键：
 *    它让脚本**不要**自动跑 init()（否则会去 fetch 网络）。
 *
 * ⚠️ 本夹具的断言与 Python 侧的 ``tests/test_v390_release.py``
 *    **逐条对拍** —— 前后端必须同算法。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const SOURCE = path.join(ROOT, "src", "gui", "static", "dashboard.js");

let passed = 0;
let failed = 0;

/**
 * 断言。
 *
 * @param {boolean} condition 条件。
 * @param {string} label 失败时的说明。
 * @returns {void}
 */
function check(condition, label) {
  if (condition) {
    passed += 1;
    return;
  }
  failed += 1;
  console.error("FAIL: " + label);
}

/**
 * 造一个极简的 localStorage 替身。
 *
 * @returns {Object} 假 localStorage。
 */
function makeStorage() {
  const map = {};
  return {
    getItem: (key) => (Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null),
    setItem: (key, value) => {
      map[key] = String(value);
    },
    removeItem: (key) => {
      delete map[key];
    }
  };
}

/**
 * 造一个极简的 DOM 替身。
 *
 * @returns {Object} 假 document。
 */
function makeDocument() {
  const noop = () => {};
  const attributes = {};
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
    body: {
      appendChild: noop,
      removeChild: noop,
      setAttribute: (key, value) => {
        attributes[key] = value;
      },
      getAttribute: (key) =>
        Object.prototype.hasOwnProperty.call(attributes, key)
          ? attributes[key]
          : null
    },
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
    window: { __SENTINEL_ROUTING__: true, localStorage: makeStorage() },
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
const log = win.SentinelPage && win.SentinelPage.log;
const mobile = win.SentinelPage && win.SentinelPage.mobile;

if (!log) {
  throw new Error("dashboard.js 没有暴露 window.SentinelPage.log");
}
if (!mobile) {
  throw new Error("dashboard.js 没有暴露 window.SentinelPage.mobile");
}

/* ------------------------------------------------------------------ */
/* 任务二：极限摘要的代码块剥离                                        */
/* ------------------------------------------------------------------ */

check(
  typeof log.stripCodeBlocks === "function",
  "log.stripCodeBlocks 必须是函数"
);
check(
  typeof log.firstSentences === "function",
  "log.firstSentences 必须是函数"
);
check(log.extremeSentences === 2, "100 档只取前两句（LOG_EXTREME_SENTENCES = 2）");

const stripped = log.stripCodeBlocks(
  "先说结论。\n\n```python\nprint('hi')\n```\n\n再说细节。"
);
check(stripped.indexOf("```") === -1, "围栏标记必须被剥掉");
check(stripped.indexOf("print") === -1, "代码块内容必须被剥掉");
check(stripped.indexOf("先说结论。") >= 0, "自然语言要保留");
check(stripped.indexOf("再说细节。") >= 0, "自然语言要保留");

const unclosed = log.stripCodeBlocks("前面这段保留。\n```\n后面的都不要");
check(unclosed.indexOf("后面") === -1, "未闭合围栏之后的全部丢掉");
check(unclosed.indexOf("前面这段保留。") >= 0, "未闭合围栏之前的内容保留");

const inline = log.stripCodeBlocks("使用 `code` 引用一段文字。");
check(inline.indexOf("`code`") >= 0, "行内反引号不算围栏");

const twoSentences = log.firstSentences("第一句。第二句。第三句。第四句。", 2);
check(twoSentences === "第一句。第二句。", "中文只取前两句");

const english = log.firstSentences(
  "I touched engine.py and ran v1.5 checks. It is fine.",
  1
);
check(english.indexOf("engine.py") >= 0, "英文不能把 engine.py 劈开");
check(english.indexOf("v1.5") >= 0, "英文不能把 v1.5 劈开");

/* 极限档的实际接线：先剥代码块，再取前两句。 */
const sourceText = fs.readFileSync(SOURCE, "utf8");
check(
  /stripLogCodeBlocks\(text\)/.test(sourceText),
  "极限档必须先调 stripLogCodeBlocks"
);
check(
  /LOG_EXTREME_SENTENCES/.test(sourceText),
  "极限档必须用 LOG_EXTREME_SENTENCES 控制句数"
);

/* ------------------------------------------------------------------ */
/* 任务四：移动端 Tab                                                  */
/* ------------------------------------------------------------------ */

check(
  mobile.tabs.length === 4,
  "移动端必须有四个 Tab（实际 " + mobile.tabs.length + " 个）"
);
check(
  mobile.tabs.join(",") === "alerts,audit,log,settings",
  "四个 Tab 的名字必须固定"
);
check(mobile.defaultTab === "alerts", "兜底 Tab 是 alerts");

check(mobile.read() === "alerts", "没有记忆时读回兜底 Tab");

const applied = mobile.apply("log");
check(applied === "log", "切到 log 应当生效");
check(
  win.document.body.getAttribute("data-mobile-tab") === "log",
  "body 上的 data-mobile-tab 必须被改写"
);

const bogus = mobile.apply("不存在的Tab");
check(bogus === "alerts", "非法 Tab 名退回兜底");
check(
  win.document.body.getAttribute("data-mobile-tab") === "alerts",
  "非法 Tab 也要把 body 属性改回兜底"
);

mobile.apply("audit");
check(mobile.read() === "audit", "切过的 Tab 要能被记住");

/* ------------------------------------------------------------------ */
/* 结果                                                                */
/* ------------------------------------------------------------------ */

console.log("v390 前端夹具：" + (passed + failed) + " 项，" + passed + " 通过");
if (failed > 0) {
  console.error("失败 " + failed + " 项");
  process.exit(1);
}
