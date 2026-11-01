/* v3.6.0 前端逻辑的**可执行**测试夹具。
 *
 * 为什么要有这个
 * ==============
 *
 * v3.6.0 的核心是「日志渲染档位」「路径校验」「拖拽布局」——
 * 这些逻辑全在浏览器里，光靠「文件里有没有这个字符串」这种弱断言
 * 等于没测：把函数体改成 return null 也能照样通过。
 *
 * 所以这里跟 v3.5.1 一样用 Node 的 ``vm`` 模块造一个**极简 DOM**，
 * 把 dashboard.js 真的**跑起来**，
 * 再调用它露出来的 ``window.SentinelPage.log`` /
 * ``window.SentinelPage.radar`` /
 * ``window.SentinelPage.flow``。
 *
 * ⚠️ ``window.__SENTINEL_ROUTING__ = true`` 是关键：
 *    它让脚本**不要**自动跑 init()（否则会去 fetch 网络）。
 *
 * ⚠️ 这里验的是**纯函数**的行为 ——
 *    拖拽本身要真 DOM 与指针事件，那是浏览器里的事，
 *    由 ``tests/test_v360_render_scale_and_split_panes.py``
 *    做**契约**断言（元素在不在、变量在不在），
 *    真实拖拽效果由演示脚本截图取证。
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
const radar = window.SentinelPage.radar;
const flow = window.SentinelPage.flow;

if (!log || !radar || !flow) {
  throw new Error("dashboard.js 没有暴露 window.SentinelPage.log / radar / flow");
}

/* --- 用例 ---------------------------------------------------------- */

const cases = [];

function add(name, got, want) {
  cases.push({ name: name, got: got, want: want });
}

/* --- 一、档位分段 ------------------------------------------------- */

add("0 档 = 纯代码", log.mode(0), "code");
add("30 档 = 纯代码", log.mode(30), "code");
add("31 档 = 自然语言", log.mode(31), "natural");
add("50 档 = 自然语言", log.mode(50), "natural");
add("69 档 = 自然语言", log.mode(69), "natural");
add("70 档 = 高度摘要", log.mode(70), "summary");
/* ⚠️ v3.7.0 起 100 档**多了一段**「极限摘要」——
 *    连续同类动作会**真的合并**成一行，不再只是「高度摘要」。
 *    合并算法本身的用例在 ``tests/fixtures/v370_frontend_harness.js``。 */
add("100 档 = 极限摘要", log.mode(100), "extreme");
add("非法档位退回默认", log.mode("abc"), "natural");

add("0 档标签", log.label(0), "纯代码");
add("50 档标签", log.label(50), "自然语言");
add("100 档标签", log.label(100), "极限摘要");

/* --- 二、真实日志形态解析 ---------------------------------------- */

// mcode 续接：上下文摘要。
add(
  "mcode 续接 = 上下文摘要",
  log.parse(
    JSON.stringify({
      message: {
        role: "compactionSummary",
        summary: "## Goal\n\n重构鉴权中间件"
      }
    })
  ).kind,
  "context"
);

// mcode 新会话：用户提问。
add(
  "mcode 新会话 = 用户提问",
  log.parse(
    JSON.stringify({
      message: {
        role: "user",
        content: [{ type: "text", text: "帮我看看鉴权中间件" }]
      }
    })
  ).kind,
  "user"
);

// codex：response_item 包装。
add(
  "codex response_item = 用户提问",
  log.parse(
    JSON.stringify({
      type: "response_item",
      payload: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "跑一下测试" }]
      }
    })
  ).kind,
  "user"
);

// claude：type=user + message。
add(
  "claude type=user = 用户提问",
  log.parse(
    JSON.stringify({
      type: "user",
      message: { role: "user", content: "改一下文档" }
    })
  ).kind,
  "user"
);

// v0.6：item.completed 包装。
add(
  "v0.6 item.completed = 用户提问",
  log.parse(
    JSON.stringify({
      type: "item.completed",
      item: {
        type: "user_message",
        content: [{ type: "text", text: "打包 exe" }]
      }
    })
  ).kind,
  "user"
);

// 扁平形态。
add(
  "扁平 user_message = 用户提问",
  log.parse(JSON.stringify({ type: "user_message", content: "扫一遍日志" })).kind,
  "user"
);

// Agent 回答。
add(
  "text = Agent 回答",
  log.parse(JSON.stringify({ type: "text", text: "已经改好了" })).kind,
  "reply"
);

// 思考。
add(
  "thinking = 思考",
  log.parse(JSON.stringify({ type: "thinking", text: "让我想想" })).kind,
  "think"
);

// 工具调用。
add(
  "tool_call = 工具调用",
  log.parse(
    JSON.stringify({
      type: "tool_call",
      tool: { name: "bash", arguments: '{"command":"ls -la"}' }
    })
  ).kind,
  "tool"
);

// 工具结果。
add(
  "tool_result = 工具结果",
  log.parse(JSON.stringify({ type: "tool_result", content: "a.txt" })).kind,
  "tool_result"
);

// 非 JSON 行。
add("非 JSON 行保持原样", log.parse("这不是 JSON").kind, "plain");

// 认不出来的合法 JSON。
add("认不出的 JSON = 其他", log.parse('{"foo":1,"bar":2}').kind, "other");

// 噪声：整条都是 environment_context。
add(
  "整条噪声被判为噪声",
  log.parse(
    JSON.stringify({
      type: "user_message",
      content: "<environment_context>\n<cwd>D:\\项目</cwd>\n</environment_context>"
    })
  ).noise,
  true
);

// 噪声 + 真问题：只挖掉噪声段落，问题必须留下。
add(
  "噪声段落被挖掉、问题留下",
  log.parse(
    JSON.stringify({
      type: "user_message",
      content:
        "<environment_context>\n<cwd>D:\\项目</cwd>\n</environment_context>\n真正的用户问题"
    })
  ).text,
  "真正的用户问题"
);

// v3.6.0 实机坑：<cwd> 有时单独出现（不在 environment_context 里），
// 流水气泡里不能只剩半截框架标签。
add(
  "单独的 cwd 标签也被挖掉",
  log.parse(
    JSON.stringify({
      type: "user_message",
      content: "<cwd>D:\\项目\\<项目目录></cwd>\n真正的问题在这"
    })
  ).text,
  "真正的问题在这"
);

/* --- 三、各档位显示什么 ------------------------------------------- */

const userEntry = log.parse(JSON.stringify({ type: "user_message", content: "问题" }));
const thinkEntry = log.parse(JSON.stringify({ type: "thinking", text: "想法" }));
const toolEntry = log.parse(
  JSON.stringify({
    type: "tool_call",
    tool: { name: "bash", arguments: '{"command":"ls"}' }
  })
);
const toolResultEntry = log.parse(
  JSON.stringify({ type: "tool_result", content: "很长很长的一段结果" })
);

add("自然语言显示思考", log.visible(thinkEntry, "natural"), true);
add("自然语言藏起工具结果", log.visible(toolResultEntry, "natural"), false);
add("高度摘要藏起思考", log.visible(thinkEntry, "summary"), false);
add("高度摘要保留用户提问", log.visible(userEntry, "summary"), true);
add("高度摘要保留工具调用", log.visible(toolEntry, "summary"), true);
add("高度摘要藏起工具结果", log.visible(toolResultEntry, "summary"), false);
add("纯代码不看事件类型", log.visible(toolResultEntry, "code"), true);

/* --- 四、正文里绝不能出现 arguments ------------------------------- */

add(
  "工具正文只有工具名、没有 arguments",
  log.body(toolEntry, "natural"),
  "bash"
);
add(
  "工具正文在摘要档也一样只有工具名",
  log.body(toolEntry, "summary"),
  "bash"
);

add(
  "用户正文就是问题本身",
  log.body(userEntry, "natural"),
  "问题"
);

/* --- 五、标签 ----------------------------------------------------- */

add("用户标签", log.tag("user"), "🧑 用户提问：");
add("回答标签", log.tag("reply"), "🤖 Agent 回答：");
add("思考标签", log.tag("think"), "💭 思考：");
add("工具标签", log.tag("tool"), "🔧 工具调用：");

/* --- 六、JSON 高亮分词 -------------------------------------------- */

const tokens = log.jsonTokens('{"a":1,"b":"x","c":true}');
add(
  "JSON 高亮分出键 / 数字 / 字符串 / 布尔",
  tokens.map((item) => item.type).join(","),
  "punct,key,plain,number,punct,key,plain,string,punct,key,plain,bool,punct"
);
add(
  "JSON 高亮保留原文",
  tokens.map((item) => item.text).join(""),
  '{"a":1,"b":"x","c":true}'
);

/* --- 七、雷达空态（路径存在与否的两种分支）----------------------- */

add(
  "文件不存在时只给相对路径",
  radar.commandFor(false, "D:\\项目\\<项目目录>\\logs\\capture.jsonl"),
  'mcode exec "你的指令" > logs/capture.jsonl'
);
add(
  "文件不存在时即使知道绝对路径也不给",
  radar.commandFor(false, ""),
  'mcode exec "你的指令" > logs/capture.jsonl'
);
add(
  "文件存在时用已验证的绝对路径",
  radar.commandFor(true, "D:\\项目\\<项目目录>\\logs\\capture.jsonl"),
  'mcode exec "你的指令" > D:\\项目\\<项目目录>\\logs\\capture.jsonl'
);
add("缺失提示语", radar.noteMissing, "请先运行该命令，生成文件后再点击接入。");
add("就绪提示语", radar.noteReady, "跑起来后点上方【＋ 发现/挂载进程】接入。");
add("相对路径常量", radar.relativePath, "logs/capture.jsonl");

/* --- 八、流水区的「核心上下文」判定 ------------------------------ */

add("用户提问算核心上下文", flow.entryKind({ event_type: "user_message" }), "user");
add("Agent 回答算核心上下文", flow.entryKind({ event_type: "text" }), "reply");
add("工具调用不算核心上下文", flow.entryKind({ event_type: "tool_call" }), "");
add("告警不算核心上下文", flow.entryKind({ type: "alert" }), "");

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
  "v3.6.0 前端逻辑用例：" + (cases.length - failed) + "/" + cases.length + " 通过\n"
);
if (failed) {
  process.exitCode = 1;
}
