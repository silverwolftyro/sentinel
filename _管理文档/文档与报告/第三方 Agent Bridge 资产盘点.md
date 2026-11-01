# 第三方 Agent Bridge 资产盘点与对接评估报告

> **盘点对象**：`third_party_agent_bridge/n.e.k.o_plugin_agent_bridge-main`
> （上游 `github.com/addsas222/n.e.k.o_plugin_agent_bridge`）
>
> **盘点性质**：**纯只读代码考古**。本次没有修改上游目录下的任何文件，
> 也没有修改哨兵现有的任何业务代码。本报告是本次唯一的写入产物。
>
> **本文档定位**：面向「哨兵未来要不要吃下 agent_bridge 的资产、怎么吃、
> 要花多少代价」的决策参考，不是使用手册。

---

## 0. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 前端能不能直接抄？ | **能，但只抄「结构 + 逻辑」，不抄配色。** `webui/index.html` 是 1034 行**零依赖**单文件（220 行 CSS + 701 行 JS），无框架、无构建、无 npm、无 CDN、无外部字体。三个目标组件（派发表单 / 状态列表 / 历史看板）全部在原生 JS 里，可以整段搬。 |
| Agent 配置能不能直接转 JSON/YAML？ | **能，而且这是最划算的一笔。** 14 个 Agent 的配置是**纯数据**（`AgentSpec` frozen dataclass + TOML 清单），`bridge/` 不 import 宿主 SDK，可零依赖 vendor。**但有 3 个坑必须先处理**（见 §4.6）。 |
| 最轻量的接管通信方式？ | **不是改它的 HTTP，也不是监听它的日志，而是「哨兵自己按它的 AgentSpec 拉起 Agent 接管管道」。** 上游的 `/api/dispatch` 是同步黑盒（任务结束才返回，中途零可观测），`dispatch.jsonl` 是**事后账本**（且两个写入者 schema 不一致）。哨兵需要的是流式，只能自己拉。详见 §5.4。 |
| 总代价 | **数据层 0.5 天 + 派发层 1 天 + 解析层 2–3 天 + 纯文本 Agent 兜底 1–2 天 ≈ 4–6 个工作日**，另加 6–7 小时逐 CLI 真机校准（**不能省**）。详见 §6。 |
| 最具复用价值的 3 个模块 | **① `bridge/registry.py` 的 `AgentSpec` 数据模型 ② `bridge/provider_manifest.py` 的数据化注册协议 ③ `bridge/runner.py` 的子进程派发与鉴权识别。** 详见 §7。 |

---

## 1. 盘点范围与方法

### 1.1 实际读过的文件

| 文件 | 字节 | 读法 | 作用 |
| --- | --- | --- | --- |
| `bridge/registry.py` | 28627 | 全文 | AgentSpec / AGENT_SPECS / ALIASES / shim 解析 / `build_argv` |
| `bridge/provider_manifest.py` | 8873 | 全文 | provider 清单 schema 与校验 |
| `bridge/providers.py` | 4883 | 全文 | 清单 → AgentSpec 的适配层 |
| `bridge/runner.py` | 25197 | 全文 | 子进程派发、鉴权识别、超时、输出限幅 |
| `bridge/config.py` | 6522 | 全文 | 配置模型与 MSYS 路径归一 |
| `bridge/tools.py` | 2036 | 全文 | `write_audit`（dispatch.jsonl 写入） |
| `webui/index.html` | 46017 | 全文 | 前端单文件（1034 行） |
| `webui/server.py` | 27960 | 全文 | HTTP API（10 个接口） |
| `webui/DESIGN.md` | 5160 | 全文 | 前端设计规范 |
| `providers/*/provider.toml` | 5 份 | 全文 | 5 条数据化路由声明 |
| `skill/neko-bridge/bin/neko.py` | 62614 | 定点 | skill 侧的独立实现与审计写入对照 |
| `__init__.py` | — | 定点 | 插件侧审计写入（与 webui 的 schema 差异） |
| `docs/agent-routing.md` | 16139 | 结构 + 前 90 行 | 作者自述的路由机制与已知问题 |
| `ui/panel.tsx` | 4068 | 全文 | 宿主内 TSX 面板（与 webui 平行的那套 UI） |

### 1.2 唯一一次「执行」，做了什么

只做了一件可观测的事：**在哨兵仓库内用 `python -B` 导入上游的
`bridge.registry`，把 `AGENT_SPECS` 导出成 JSON**（产物
`logs/agentbridge-audit/registry_dump.json`）。

- 用 `-B` + `PYTHONDONTWRITEBYTECODE=1` 运行，**确认上游目录内 `__pycache__` 数量为 0**；
- 该 import 只读 `providers/*/provider.toml`，不发起任何子进程、不联网、不写上游目录。

这样拿到的 14 条配置是**代码实际产出的结果**，而不是我从源码里手抄的。

### 1.3 本次盘点**没有**做的事（诚实边界）

- **没有真机跑过任何一个 Agent CLI**。本机没装这些 CLI，agent_bridge 插件也没在本机运行过
  （`~/.neko-agent-bridge/` 目录不存在）。
  因此文中所有「实测」「已验证」字样，**若指向某个具体 CLI 的行为，都是上游源码里作者的自述**，
  不是本次盘点的观测结果。凡属本次实际观测的，均标注「本次实测」。
- **没有拿到 `dispatch.jsonl` 的真实样本**。其结构是**从代码推导**的（§5.1），
  推导依据写明了每一行的出处。
- 上游仓库自带的 `tests/` 未执行（会真跑 CLI）。

---

## 2. 项目结构速览

```
n.e.k.o_plugin_agent_bridge-main/
├── plugin.toml                插件清单（4 个 entry + [bridge] 业务段）
├── __init__.py                插件类：@plugin_entry / @llm_tool / @ui.context
├── bridge/                    ★ 执行层（纯标准库，不 import plugin.sdk）
│   ├── registry.py            AgentSpec + AGENT_SPECS + .cmd shim 解析 + build_argv
│   ├── runner.py              子进程派发 / 鉴权识别 / 超时 / 输出限幅
│   ├── provider_manifest.py   provider 清单 schema（零依赖，可 vendor）
│   ├── providers.py           清单 → AgentSpec 的适配层
│   ├── config.py              BridgeConfig + 逐 Agent 覆盖 + MSYS 路径归一
│   └── tools.py               结果排版 + jsonl 审计
├── webui/                     ★ 本地控制台（纯标准库 HTTP + 单文件前端）
│   ├── index.html             1034 行单文件前端
│   ├── server.py              ThreadingHTTPServer，10 个 JSON 接口
│   └── DESIGN.md              工业仪表盘设计规范
├── providers/                 ★ 数据化路由声明（5 个）
│   ├── command-code/gemini/hermes/pi/step/provider.toml
├── skill/neko-bridge/         反向 skill（把 N.E.K.O 装进别的 Agent）
│   ├── bin/neko.py            ★ 自带一份 AGENT_SPECS（手抄的第二份）
│   ├── frameworks.toml        66 行 harness 矩阵
│   └── providers/             ⚠️ providers/ 的**第二份副本**
├── ui/panel.tsx               宿主内 TSX 面板（@neko/plugin-ui）
└── docs/agent-routing.md      作者自述的结构分析与接入方案
```

**三个值得注意的结构事实**（都影响对接成本）：

1. `bridge/` **不 import `plugin.sdk`** —— 所以它能脱离宿主跑单测，
   也意味着**哨兵可以零依赖 vendor 这 5 个模块**，不需要为了用配置而引入整个插件。
2. `providers/` 与 `skill/neko-bridge/providers/` 是**两份副本**。
   转换时以 `providers/` 为准（本报告的数据即取自 `providers/`）。
3. `skill/neko-bridge/bin/neko.py` 里还有**第三份** `AGENT_SPECS` 字面量。
   作者自己在 `docs/agent-routing.md` §4.1 把它列为「两张手抄的规格表」问题。

---

## 3. 任务一：前端展示窗资产盘点（UI & UX）

### 3.1 架构定性：**零依赖单文件**

对 `webui/index.html` 全文的机械核验结果：

| 指标 | 实测值 |
| --- | --- |
| 总行数 | 1034 |
| CSS 块 | 1 个 `<style>`，220 行（行 7–220） |
| JS 块 | 1 个 `<script>`，701 行（行 331–1031） |
| `<script src>` / `<link>` / `import` / `require` / CDN / `http://` | **全部 0 处** |
| 用到的浏览器 API | `fetch`、`localStorage`、`AbortController`、`navigator.clipboard` |
| JS 语言特性 | `const` / 箭头函数 / 模板字符串 / 可选链 `?.` / 空值合并 `??` / `closest()` |

**结论：纯原生 HTML + CSS + 现代 JS，可以整段复制进哨兵，不需要任何构建工具、
不需要 npm install、不需要框架适配。** 这是本次盘点里复用成本最低的一块资产。

> ⚠️ `ui/panel.tsx` 是**另一套** UI（宿主内的 React/TSX 面板），依赖
> `@neko/plugin-ui` 的 `Page/Stack/Card/ActionForm` 组件，**框架耦合，不可直接复用**。
> 哨兵要抄的是 `webui/` 这套。

### 3.2 组件 A：任务派发表单

**关键设计决策（决定了复用方式）**：Agent **不是用下拉框选的**。
`webui/DESIGN.md` §5 写得很直白：

> 左栏是**唯一的主界面**：表格就是导航，点行 = 选中，不需要下拉框选 Agent。

也就是说，**「选 Agent」和「填 Prompt」在空间上是分离的**：先在左栏表格点一行，
右栏的派发表单随之激活。这对哨兵是好消息 —— 哨兵的雷达面板本来就是
「列表 + 详情」结构，可以直接沿用这个交互，不需要额外做 select 控件。

**HTML 结构**（`webui/index.html:287-305`，可直接抄）：

```html
<div class="block" id="dispatch-block">
  <h2>派发任务</h2>
  <textarea id="task" placeholder="任务描述（自包含）。Enter 发送，Shift+Enter 换行。"></textarea>
  <div class="row">
    <label for="cwd">cwd</label>
    <input id="cwd" type="text" placeholder="留空=默认工作目录" spellcheck="false">
  </div>
  <div class="row">
    <label for="timeout">超时</label>
    <div class="slider" style="flex:1">
      <input id="timeout" type="range" min="5" max="3600" step="5" value="300">
      <span class="val" id="timeout-val">5:00</span>
    </div>
  </div>
  <div class="row">
    <button class="act primary" id="run" disabled style="flex:1">派发 Enter</button>
    <button class="act" id="toggle" disabled title="切换该 Agent 的 enabled">禁用 e</button>
  </div>
</div>
```

**三个值得抄的细节**：

1. **空态不显示禁用表单**：未选中 Agent 时，`#detail-empty` 显示一句话
   （"选中一行，按 Enter 派发"），派发按钮保持 `disabled`。
   哨兵现有 UI 若习惯「永远显示表单」，可以改成这个模式。
2. **按钮文本自带快捷键提示**（"派发 Enter"、"禁用 e"），且快捷键真实存在
   （`j/k` 移动、`Enter` 派发、`/` 过滤、`r` 刷新、`e` 启停，见 `index.html:825-840`）。
3. **超时用 range 滑条而不是输入框**，最小 5 秒、最大 3600 秒、步长 5 秒。

**派发逻辑**（`index.html:765-798`，可直接抄）：

```javascript
async function doDispatch(){
  const a = state.agents.find(x=>x.id===state.sel);
  if (!a || state.running) return;
  const task = $("task").value.trim();
  if (!task){ toast("任务不能为空"); $("task").focus(); return; }
  state.running = true;
  $("run").disabled = true;
  const t0 = Date.now();
  $("out-meta").textContent = `派发中 · ${a.id} · 0.0s`;
  $("out").textContent = "";
  // 200ms 心跳：让用户看到「在跑」而不是「卡住」
  const tick = setInterval(()=>{
    $("out-meta").textContent = `派发中 · ${a.id} · ${((Date.now()-t0)/1000).toFixed(1)}s`;
  }, 200);
  try{
    const r = await api("/api/dispatch", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        agent:a.id, task, cwd:$("cwd").value.trim(),
        timeout_seconds:Number($("timeout").value)||0
      })
    });
    clearInterval(tick);
    const ok = r.ok;
    $("out-meta").innerHTML = `${ok?"<span style='color:var(--ok)'>OK</span>":"<span style='color:var(--bad)'>失败</span>"} · ${esc(r.agent||"")} · exit=${r.exit_code ?? "–"} · ${r.seconds ?? ((Date.now()-t0)/1000).toFixed(1)}s`;
    $("out").textContent = [r.error ? `! ${r.error}` : "", r.output || "(无输出)",
      r.stderr ? `--- stderr ---\n${r.stderr}` : ""].filter(Boolean).join("\n");
    if (r.needs_login) { toast("该 CLI 未登录，已标记并跳过"); await loadAgents(); }
  }catch(e){
    clearInterval(tick);
    $("out-meta").textContent = "请求失败";
    $("out").textContent = String(e);
  }finally{
    state.running = false;
    $("run").disabled = false;
    loadAudit();
    $("out").scrollTop = $("out").scrollHeight;
  }
}
```

**哨兵侧复用注意**：这段代码是**一次性请求-响应**模型（`await` 到任务结束），
因为上游的 `/api/dispatch` 同步阻塞（§5.2）。哨兵的真接管是**流式**，
这段的 `tick` 心跳 + `finally` 收尾可以保留，但 `$("out").textContent` 的
一次性赋值要换成**逐行追加**。

### 3.3 组件 B：Agent 状态列表

**状态机**（`index.html:336-355`，可直接抄）：

```javascript
const STATUS = {
  ok:    {cls:"ok",   text:"可派发"},
  warn:  {cls:"warn", text:"需登录"},
  bad:   {cls:"bad",  text:"失败"},
  idle:  {cls:"idle", text:"没装"},
};

function statusOf(a){
  if (!a.exists) return {key:"idle", text:"没装"};
  if (!a.enabled) return {key:"idle", text:"已禁用"};
  if (a.needs_login) return {key:"warn", text:"需登录"};
  // 慢 ≠ 不可用：有的 CLI 未登录时静默挂起（step 实测 40s+ 零输出），
  // 单独一档，别让用户以为它能正常干活
  if (a.slow) return {key:"warn", text:"慢/待验证"};
  return {key:"ok", text:"可派发"};
}
function chip(st){
  const c = STATUS[st.key] || STATUS.idle;
  return `<span class="chip st-${c.cls}"><i class="sq ${c.cls}"></i>${st.text}</span>`;
}
```

**⚠️ 重要澄清：Agent 行上并没有 `running` 状态。**

任务书里的「ready / running」在本项目里是这样落地的：

| 概念 | 本项目的实现 | 出处 |
| --- | --- | --- |
| ready | `statusOf()` 返回 `ok`（可派发）；等价于后端的 `ready = exists && enabled && !needs_login` | `index.html:343` / `server.py:120` |
| running | **不在 Agent 行上**。只在派发按钮的 `disabled` 态 + `#out-meta` 的计时器里体现 | `index.html:770-777` |
| 上次结果 | 表格「上次」列（`state.lastRun[id]`），来自审计日志 | `index.html:462-467` |

所以：**「running 状态列表」这个组件本项目并没有做**。如果哨兵要做
「实时显示哪些 Agent 正在跑」，需要自己加 —— 但数据来源现成：
`/api/dispatch` 是同步的，上游**不提供**「当前在跑的任务」查询接口。
哨兵只能靠自己的进程树扫描（`src/proc_tree.py` 已经在做了）来推断。

**最值得抄的一条设计**：`slow` 独立成档（"慢/待验证"）。
作者的注释说得很清楚 —— 有的 CLI 未登录时**静默挂起**（step 40 秒零输出），
如果不把它和「可派发」分开，用户会以为它能干活。**这个坑哨兵也要踩**，
哨兵雷达里「进程活着但没输出」和「进程正常在跑」目前可能混为一谈。

**列宽自适应**（`index.html:392-414`）也有一个值得抄的注释：

```javascript
/** 当前该显示哪些列。窄屏靠**减少列**，不靠隐藏单元格 ——
 *  隐藏 td 而表头不跟着少，fixed 布局下后面的单元格会左移，
 *  表现就是「别名里显示命令、命令里显示版本」。 */
function visibleCols(){
  const w = window.innerWidth;
  return allCols().filter(c => !c.hideBelow || w >= c.hideBelow);
}
```

**表头与单元格同源**（`renderHead()` 与 `cellHtml()` 共用 `visibleCols()`）
是这套表格不串列的根本原因，抄的时候**必须一起抄**。

### 3.4 组件 C：历史任务看板

位于 `<footer>`（`index.html:312-329`），一个高度可调的审计条。
两个交互细节是真正的价值所在：

**① 跟随锁定 —— 往下翻自动暂停**

```javascript
$("audit-scroll").addEventListener("scroll", ()=>{
  const atTop = $("audit-scroll").scrollTop <= 4;
  if (atTop === state.follow) return;
  state.follow = atTop;          // 往下翻 = 看历史，自动解锁；回到顶部 = 自动锁定
  syncFollow();
});
```

**② 只改单元格，不整表重绘**（`index.html:733-734, 752-762`）：

```javascript
// 暂停跟随时不重建 DOM：否则用户正在看历史，列表被换掉、滚动位置也丢
if (!state.follow && $("audit").childElementCount > 0) { refreshLastCells(); return; }
```

```javascript
/** 只改「上次」列，不整表重绘 —— 每 15s 翻新审计时不能让用户滚动位置跳走。 */
function refreshLastCells(){
  document.querySelectorAll('#body tr[data-id] td.last').forEach((td)=>{
    const id = td.parentElement.dataset.id;
    const last = state.lastRun[id];
    td.innerHTML = last
      ? `<span class="st-${last.ok ? "ok" : (last.needs_login ? "warn" : "bad")}">${last.ok ? "OK" : (last.needs_login ? "需登录" : "失败")}</span> ${last.time} · ${last.seconds}s`
      : `<span style="color:var(--idle)">—</span>`;
    td.title = last ? (last.task || "") : "本面板还没派发过";
  });
}
```

**哨兵侧对应**：哨兵雷达面板的进程列表是**周期性刷新**的，
如果整表重绘，用户滚动位置会跳。这段「局部更新」的模式直接可用。

**数据加载**（`loadAudit()`，`index.html:714-750`）：15 秒轮询
`/api/audit?limit=40`，倒序渲染（最新在上）。记录是**倒序**的，
所以「每个 Agent 的上次结果」取第一条命中：

```javascript
for (const r of rows){
  const id = r.agent;
  if (!id || state.lastRun[id]) continue;   // rows 是倒序，第一条就是最新
  const ok = r.ok === true || (r.exit_code === 0 && !r.needs_login && !r.timed_out);
  ...
}
```

> `ok` 那行的兜底表达式**不是多余的** —— 它在兼容两种 schema（§5.1）。

### 3.5 复用难度评估

| 组件 | 框架耦合 | 外部依赖 | 复用难度 | 需要改什么 |
| --- | --- | --- | --- | --- |
| 派发表单（HTML + `doDispatch`） | **无** | 仅 `fetch` | **低** | 换后端接口；流式版要改输出区写法 |
| Agent 状态列表（`statusOf` / `chip` / `visibleCols` / `cellHtml`） | **无** | 无 | **低** | 换 CSS 变量；`statusOf` 的入参字段名要对齐哨兵 |
| 历史任务看板（`loadAudit` / `refreshLastCells` / 跟随锁定） | **无** | 仅 `fetch` | **低** | 换审计数据源；哨兵侧数据是**自己产生的**，字段名不同 |
| 设计令牌 CSS（220 行） | 无 | 无 | **中** | 哨兵是暗色主题，这套是暖灰亮色。**建议只抄结构类名，不抄配色变量** |
| `ui/panel.tsx` | **React + `@neko/plugin-ui`** | 宿主 SDK | **不可复用** | 与宿主面板体系绑定 |
| HARNESS 页签（`/api/harness` + `frameworks.toml` 66 行矩阵） | 无 | 无 | **中** | 是「把 N.E.K.O 装进别的 Agent」的装机矩阵，与哨兵审计业务无直接关系 |

**总评：前端整体是「零依赖单文件」，三个目标组件都可以直接抄。**
唯一的实质性工作是**换配色**和**换后端字段名**。

### 3.6 与上游后端的耦合点清单（抄之前必须知道的 10 个接口）

| 接口 | 方法 | 前端用在哪 | 哨兵侧对应 |
| --- | --- | --- | --- |
| `/api/agents` | GET | `loadAgents()` 行 692 | 哨兵已有 `/api/discover`（雷达） |
| `/api/version` | GET | `probeVersion()` 行 570 | 哨兵无（可从 `/api/discover` 扩展） |
| `/api/dispatch` | POST | `doDispatch()` 行 779 | **哨兵要自己接管，不要走这个** |
| `/api/enabled` | POST | `toggleEnabled()` 行 803 | 哨兵无「禁用某 Agent」概念 |
| `/api/audit` | GET | `loadAudit()` 行 716 | 哨兵有自己的会话日志 |
| `/api/harness` | GET | `loadHarness()` 行 708 | 哨兵无（不需要） |
| `/api/install` | POST | `doInstall()` 行 626 | 哨兵无 |
| `/api/install_skill` | POST | `doInstallSkill()` 行 641 | 哨兵无 |
| `/api/locate` | POST | `doLocate()` 行 660 | 哨兵无（但**哨兵需要等价物**：指定 exe 路径） |
| `/api/health` | GET | `checkHealth()` 行 672 | 哨兵有 `/api/state` 一类 |

**另外有一个必须一起抄的机制 —— 前后端版本握手**（`server.py:47-49`）：

```python
BUILD = "r5"
"""服务端构建号。页面会拿它比对 —— 前后端版本不一致时，功能会「点了没反应」，
这是本仓库踩过的坑：改了 server.py 但没重启，前端拿不到新接口。"""
```

前端 `checkHealth()` 会比对 `/api/health` 的 `features` 列表，缺接口就弹红条警告。
**哨兵若采用这种「单文件前端 + 独立后端」架构，这个握手值得抄** ——
它是「改了后端忘了重启」这个经典坑的正解。

---

## 4. 任务二：Agent 适配库提取（Connectors）

### 4.1 数据模型：两套等价结构

**运行时结构**（`bridge/registry.py:43-44`）：

```python
@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent CLI 的登记信息。"""
```

字段（18 个）：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `id` | `str` | 规范 id（`^[a-z][a-z0-9-]{1,31}$`） |
| `label` | `str` | 展示名 |
| `binary` | `str` | PATH 上查找的可执行名 |
| `mode` | `str` | `flag` / `switch` / `positional` |
| `fixed_args` | `tuple[str, ...]` | 固定前缀参数 |
| `prompt_flag` | `str \| None` | 正文开关 |
| `model_flag` | `str \| None` | 模型参数名 |
| `json_args` | `tuple[str, ...]` | JSON 输出开关 |
| `version_args` | `tuple[str, ...]` | 版本探测参数（默认 `("--version",)`） |
| `notes` | `str` | 备注（**内置 9 条只有这个字段非空**） |
| `hints` | `tuple[str, ...]` | 运维提示 |
| `aliases` | `tuple[str, ...]` | 别名 |
| `auth_patterns` | `tuple[str, ...]` | 该 Agent 专属的鉴权失败措辞（正则） |
| `login_hint` | `str` | 登录指引 |
| `install_cmd` | `str` | 一键安装命令 |
| `install_hint` | `str` | 安装指引 |
| `fail_patterns` | `tuple[str, ...]` | **非鉴权**的运行期失败措辞 |

**登记协议**（`bridge/provider_manifest.py`）：`schema = 1`，
清单文件名 `provider.toml` 或 `provider.json`，`[provider]` 表必填
`id`（**必须等于目录名**）、`mode`、`binary`，`label` 可选（缺省用 id）。
`mode ∈ {flag, switch, positional}`；`flag`/`switch` **必须**声明 `prompt_flag`。

### 4.2 14 个 Agent 的完整配置表

> **本次实测导出**（`bridge.registry` 实际 import 的结果）。
> 「鉴权特征」「登录」「安装」三列大量为空 —— 原因见 §4.4，这不是导出失败。

| # | id | 名称 | binary | mode | fixed_args | prompt_flag | model_flag | json_args | 鉴权特征 | 登录 / 安装 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `atomcode` | AtomCode | atomcode | flag | — | `-p` | — | `--output-format jsonl` | *（全局兜底）* | *（空）* |
| 2 | `claude` | Claude Code（Anthropic） | claude | flag | — | `-p` | `--model` | `--output-format json` | *（全局兜底）* | *（空）* |
| 3 | `codebuddy` | CodeBuddy Code（腾讯） | codebuddy | flag | — | `-p` | `--model` | `--output-format json` | *（全局兜底）* | *（空）* |
| 4 | `command-code` | Command Code | command-code | switch | — | `-p` | `--model` | `--output-format json` | `authentication failed` / `please run .{0,12}login.{0,4} first` | `cmdc login`；`bun add -g command-code` |
| 5 | `dsh` | DeepSeek Harness | dsh | positional | `--profile headless` | — | — | — | *（全局兜底）* | *（空）* |
| 6 | `evox` | EvoX | evox | flag | `--evox-cli` | `-p` | `--model` | — | *（全局兜底）* | *（空）* |
| 7 | `gemini` | Gemini CLI（Google） | gemini | flag | — | `-p` | `-m` | `-o json` | `please set an auth method` / `specify one of the following environment variables` | 设 `GEMINI_API_KEY` 或 `~/.gemini/settings.json`；`bun add -g @google/gemini-cli` |
| 8 | `hermes` | Hermes（Nous Research） | hermes | flag | — | `-z` | `-m` | — | *（空）*；**fail_patterns**：`gateway inaccessible` / `service_manager_unavailable` | git 安装，无包管理器命令 |
| 9 | `omp` | Oh My Pi | omp | flag | — | `-p` | `--model` | `--mode json` | *（全局兜底）* | *（空）* |
| 10 | `openclaw` | OpenClaw | openclaw | positional | `agent exec` | — | `--model` | `--json` | *（全局兜底）* | *（空）* |
| 11 | `opencode` | OpenCode | opencode | positional | `run` | — | `--model` | `--format json` | *（全局兜底）* | *（空）* |
| 12 | `pi` | pi（AI coding assistant） | pi | switch | — | `-p` | `--model` | `--mode json` | `pi auth` ⚠️**推断值，作者自述未实测** | `pi auth` / `--api-key`；`bun add -g @earendil-works/pi-coding-agent` |
| 13 | `qwenpaw` | QwenPaw | qwenpaw | flag | `task` | `-i` | `-m` | — | *（全局兜底）* | *（空）* |
| 14 | `step` | StepCode CLI（阶跃） | step | switch | — | `-p` | `--model` | — | *（空）* | `step auth`；发行包安装 |

**别名表**（本次实测导出，共 16 条）：

```
cbc→codebuddy  codebuddy-code→codebuddy  oh-my-pi→omp  ompi→omp
oc→opencode    claw→openclaw              atom→atomcode  qwen→qwenpaw
cmdc→command-code  commandcode→command-code
gcli→gemini    geminicli→gemini
hermes-agent→hermes
pi-coding-agent→pi  earendil-pi→pi
stepcode→step
```

> ⚠️ **`pi` → `omp` 的旧别名已被移除**（作者在 `providers/pi/provider.toml` 注释里
> 说明了原因：`omp` 来自 `@oh-my-pi/pi-coding-agent`，`pi` 来自
> `@earendil-works/pi-coding-agent`，**两个不同产品共用可执行名 `pi`**）。
> 哨兵 `src/proc_tree.py:176-185` 的 `CLOUD_AGENT_ALIASES` 里**还没有**这 8 条新别名
> （只有内置 9 个 Agent 的别名）—— 这是一处**已存在的缺口**。

### 4.3 argv 拼装规则：**只有 2 条规则覆盖 14 个 Agent**

`build_argv()`（`bridge/registry.py:605-641`）：

```python
argv: list[str] = [resolved.exe, *resolved.argv_prefix, *fixed]

if spec.mode == "flag":
    argv += [spec.prompt_flag, task]
elif spec.mode == "switch":
    argv += [spec.prompt_flag, task]      # ← 与 flag 拼出的 argv 完全相同
elif spec.mode == "positional":
    argv.append(task)

if json_output and spec.json_args:
    argv += list(spec.json_args)
if model and spec.model_flag:
    argv += [spec.model_flag, model]

argv += [str(a) for a in extra_args]
```

**归约后的哨兵侧规则**：

```
argv = [exe] + fixed_args + (
    [prompt_flag, task]   if mode in (flag, switch)
    else [task]           if mode == positional
) + (json_args if want_json) + ([model_flag, model] if model) + extra_args
```

**`flag` 与 `switch` 的差别只是「登记时的自我约束」**，拼出的 argv 一模一样。
作者的注释说得很清楚：分开写是为了**避免把不吃值的布尔开关误写成 flag 后，
到真机上才发现**。对哨兵而言：**保留 `mode` 字段做文档与校验，但 argv 生成器
可以合并成一个分支**。

**`json_args` 在上游的 webui 派发路径里其实不生效** ——
`webui/server.py:438-447` 调 `run_agent()` 时**没有传** `json_output`，
默认 `False`，所以那 8 条 `json_args` 在控制台派发时**不会被拼进命令行**。
哨兵要接流式输出，**必须显式打开**，这就是 §4.6 的坑之一。

### 4.4 鉴权与失败语义：**两层结构**

这是本项目**最有价值的设计**，也是最容易被漏抄的部分。

**第一层：全局兜底表**（`bridge/runner.py:77-86`，8 条正则）：

```python
_AUTH_PATTERNS = (
    re.compile(r"authentication\s+required", re.IGNORECASE),
    re.compile(r"unauthenticated", re.IGNORECASE),
    re.compile(r"not\s+(?:logged\s?in|authenticated|signed\s?in)", re.IGNORECASE),
    re.compile(r"(?:please\s+)?use\s+`?/login`?", re.IGNORECASE),
    re.compile(r"(?:please\s+)?run\s+`?/login`?", re.IGNORECASE),
    re.compile(r"invalid\s+api\s*key", re.IGNORECASE),
    re.compile(r"credentials?\s+(?:expired|invalid|not\s+found|missing)", re.IGNORECASE),
    re.compile(r"请(?:先)?登录|尚未登录|未登录|需要登录|登录后(?:再|才)"),
)
```

**第二层：每 Agent 的 `auth_patterns` 扩展**（只 3 个 Agent 有：`command-code` / `gemini` / `pi`）。

**判定门槛**（`runner.py:109-124, 127-165`，`bridge/runner.py` 的 `_match_head` + `_detect_auth_failure`）：

1. stderr 必须**短**（≤ 800 字符）；
2. 只看 stderr **开头** 400 字符；
3. stdout **不能有实质内容**（`_has_substantive_output()` 滤掉警告/空行/纯符号行后，
   剩余任一行长度 ≥ 8 即视为有正文）。

**为什么必须这样**（作者在 docstring 里写得很清楚）：

> 未登录时**退出码是 0**，只往 stderr 打一行「Authentication required...」。
> 于是 `AgentResult.ok` 判定为成功，那行登录提示被当成「任务输出」原样交给上层模型 ——
> 模型只能从一段莫名其妙的输出里猜「哦原来要登录」。

而且**退出码不能当门槛**：gemini 未登录是 **41**、command-code 是 **3**、
claude 却是 **0**。

**第三层：`fail_patterns`** —— 区分「没登录」与「运行期依赖缺失」。
`hermes` 的 gateway 没起时**退出码也是 0**，只打一行
`Error: Gateway inaccessible: service_manager_unavailable`。作者把它单列为一类，
因为**处置动作不同**（去起服务，不是去登录）。

**哨兵侧必须注意的一个未消费字段**：

`providers/step/provider.toml:27` 写了 `failure_mode = "timeout"`，注释说
「未登录时挂起时**根本没有输出**，只能靠超时判定」。但是：

- `build_manifest()`（`provider_manifest.py:169-189`）**不读这个字段**；
- 全仓库只有 `tests/test_providers.py:72` 读它。

**⇒ 这是「文档 + 测试」用的标记，运行时没有任何效果。**
哨兵要实现 step 的语义，必须**自己写**「静默挂起 → 靠超时判定」这一档。

### 4.5 环境变量与登录要求

**清单里没有「环境变量」字段**。环境变量的要求是**写在 `hints` 里的自然语言**：

| Agent | 环境变量 / 凭据要求（作者自述） | 出处 |
| --- | --- | --- |
| gemini | `GEMINI_API_KEY`、`GOOGLE_GENAI_USE_VERTEXAI`、`GOOGLE_GENAI_USE_GCA`，或 `~/.gemini/settings.json` 里选 Auth 方式 | `providers/gemini/provider.toml` 的 `auth_patterns` 注释 |
| pi | `--api-key` 或环境变量；`pi auth` 查 provider 就绪状态 | `providers/pi/provider.toml` |
| codebuddy | 凭据挂在 **WorkBuddy 桌面端**（不在 CLI 上） | 内置 `notes` |
| atomcode | **AtomGit OAuth** 登录 | 内置 `notes` |
| step | `step auth` | `providers/step/provider.toml` |
| command-code | `cmdc login` | `providers/command-code/provider.toml` |
| omp / claude / dsh / evox / opencode / openclaw / qwenpaw | **未声明** | 内置 9 条的这些字段全空 |

**运行时真正生效的环境变量只有两类**（都不是「某个 Agent 需要什么 key」）：

1. **通用覆盖**（`bridge/config.py`）：`BridgeConfig.agents.<id>.env` 是
   `dict[str, str]`，在派发时通过 `env_overrides` 覆盖进子进程环境。
2. **Python 环境清洗**（`bridge/runner.py:47-54`）：派发前**统一剔除** 6 个变量：

```python
_ENV_STRIP = (
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP",
    "PYTHONEXECUTABLE", "PYTHONUSERBASE", "PYTHONLEGACYWINDOWSSTDIO",
)
```

理由：宿主跑在嵌入式 Python 3.11 上时，这些变量会让 Agent 自己的 Python
解释器（qwenpaw 的 venv 等）直接起不来。
**⚠️ 这条对哨兵同样重要 —— 哨兵打包成 exe 后也要做同样的清洗。**

另外 `_execute_blocking()`（`runner.py:356-364`）会**同时设置 `cwd` 和 `PWD`**：

```python
# 工作目录与 PWD 必须一起处理。有些 CLI 不看 cwd，而是读 PWD 环境变量来决定
# 工作目录；如果宿主是从 Git Bash / WSL 启起来的，PWD 会是 `/c/Users/...`
# 这种 MSYS 形式，CLI 拿去 chdir 会直接失败（实测 opencode 就是这么挂的）。
```

### 4.6 能否直接转 JSON/YAML 喂给哨兵？

**结论：能，且这是本次盘点里性价比最高的一笔。但有 3 个必须先处理的坑。**

#### ✅ 为什么能

1. **纯数据**：`AgentSpec` 是 frozen dataclass，没有方法、没有行为依赖，
   `build_argv()` 是**自由函数**（不绑 `self`）。
2. **零依赖**：`bridge/` 不 import 宿主 SDK；`provider_manifest.py` 只用
   `tomllib`（Python 3.11+）或 `tomli`。
3. **已有先例**：上游自己就是这么干的 —— `providers/*.toml` 是数据，
   `registry.py` 是内置表的同构体。
4. **哨兵侧有现成落点**：`_构建与发布/scripts/run_with_sentinel.py:369-397`
   的 `AGENT_PROFILES` 与 `AgentSpec` **几乎一一对应**：

```python
AGENT_PROFILES: Dict[str, AgentProfile] = {
    "mcode": AgentProfile(
        name="mcode", label="mcode",
        exec_names=["mcode", "mcode.cmd", "mcode.exe", "mcode.bat"],
        build_args=_build_mcode_args,
        adapter="mcode",
        done_markers=['"exec.completed"'],
        process_names=["node"],
    ),
    ...
}
```

#### 字段映射表

| 上游 `AgentSpec` | 哨兵 `AgentProfile` | 转换说明 |
| --- | --- | --- |
| `id` | `name` | 直取 |
| `label` | `label` | 直取 |
| `binary` | `exec_names[0]` | 哨兵是**列表**（多候选），上游是单值 → `[binary, binary+".cmd", binary+".exe"]` |
| `mode` + `fixed_args` + `prompt_flag` | `build_args` | **需要写函数**，见下 |
| `json_args` | `build_args` 内 | 哨兵必须**显式打开**（上游 webui 路径没开） |
| `model_flag` | `build_args` 内 | 直取 |
| `aliases` | 哨兵 `src/proc_tree.py` 的 `CLOUD_AGENT_ALIASES` | **当前只有 8 条，缺 8 条** |
| `auth_patterns` | **哨兵目前没有对应字段** | 需新增 |
| `login_hint` / `install_cmd` / `install_hint` / `notes` / `hints` | **哨兵目前没有对应字段** | 雷达面板可展示 |
| `fail_patterns` | **哨兵目前没有对应字段** | 需新增（区分「没登录」与「服务没起」） |
| `version_args` | 哨兵不需要 | 上游用于「探测版本」，哨兵走自己的探测 |

#### ⚠️ 坑 1：6 个 Agent **没有可直接解析的机器可读输出格式**

按「输出形态是否可直接解析」重新分组（**注意 `pi` 有 `json_args`，
但形态不可直接用**，见下）：

| 分组 | Agent | 数量 |
| --- | --- | --- |
| **A. 有 `json_args`，形态是单个 JSON 文档 / JSONL** | atomcode（`--output-format jsonl`）、claude（`--output-format json`）、codebuddy（`--output-format json`）、command-code（`--output-format json`）、gemini（`-o json`）、omp（`--mode json`）、openclaw（`--json`）、opencode（`--format json`） | 8 |
| **B. 有 `json_args`，但形态是事件流（不可直接用）** | **pi**（`--mode json`） | 1 |
| **C. 完全没有 `json_args`，只有纯文本 stdout** | **dsh、evox、hermes、qwenpaw、step** | 5 |

> `pi` 归入 B 的依据是作者在 `providers/pi/provider.toml` 的 `hints` 里明说：
> 「`--mode json` 输出的是**事件流**，取正文需要额外解析；默认别开」。
> 它确实登记了 `json_args`，但**不能当成 A 组那样写个通用 parser**。

→ **B + C 共 6 个 Agent 无法走「解析 JSON 事件流」这条路**：
B 组要写事件流状态机，C 组只能拿到纯文本。
哨兵要做 L2 流式漂移判定，这 6 个要么新写解析器（C 组还得是启发式的），
要么**退化为「整段输出事后判定」**。
这是改造成本里最大的一块，且**能力是打折的**。

#### ⚠️ 坑 2：`json_args` 默认不生效，且各 CLI 的 JSON 形态互不相同

上游的 `json_args` 只是「开关」，**没有声明输出 schema**。实测各家的形态：

- claude：哨兵现有实现用的是 `--output-format **stream-json**`，
  上游登记的是 `--output-format json`。**两者不同** ——
  哨兵现有的 stream-json 明显更适合 L1/L2 流式，**不要照抄上游的**。
- atomcode：`jsonl`（每行一个 JSON）
- omp / pi：`--mode json`（事件流，**不是单个 JSON 文档**）
- gemini：`-o json`（**单个 JSON 对象，正文在 `response` 字段**）
  —— 作者明确说「要看正文就别开 json_args」。
- opencode：`--format json`
- openclaw：`--json`
- command-code：`--output-format json`

⇒ **8 个 JSON 形态至少要写 8 个适配器**，不能指望一个通用 parser。
哨兵现有 `src/adapters/` 有 `claude.py` / `codex.py` / `mcode.py` 三个，
**`claude.py` 可以直接复用**（上游的 claude 也是 `--output-format json`，
但哨兵用的 stream-json 更好 —— 说明上游这个字段反而是**落后**的）。

#### ⚠️ 坑 3：运维元数据只有 5/14 完整

- 内置 9 个 Agent（atomcode/claude/codebuddy/dsh/evox/omp/openclaw/opencode/qwenpaw）的
  `hints` / `auth_patterns` / `login_hint` / `install_cmd` / `install_hint` **全是空的**，
  只有 `notes` 有一句话。
- 有完整元数据的只有 5 个 provider 声明的（command-code / gemini / hermes / pi / step）。

⇒ 转换时**不能假设 14 条都有鉴权特征**。哨兵要同时实现：
**全局兜底表（8 条正则） + 每 Agent 覆盖（3 条）**，两层缺一不可。

#### ⚠️ 附带坑 4：`failure_mode` 运行时不被消费（§4.4 已述）

#### ⚠️ 附带坑 5：两份 provider 副本会漂移

`providers/` 与 `skill/neko-bridge/providers/` 内容相同但是两份文件。
转换时以 `providers/` 为准；**不要同时读两份再合并**（会得到重复条目）。

---

## 5. 任务三：通信与日志机制评估

### 5.1 `dispatch.jsonl` 的记录结构

**路径**（`webui/server.py:69`）：

```python
AUDIT_PATH = CONFIG_PATH.parent / "dispatch.jsonl"
```

`CONFIG_PATH` 默认 `~/.neko-agent-bridge/config.json`
（可被环境变量 `NEKO_AGENT_BRIDGE_CONFIG` 覆盖），
所以 **默认审计日志路径 = `~/.neko-agent-bridge/dispatch.jsonl`**。
⚠️ 换配置目录时审计日志会跟着换 —— 哨兵要监听的话必须知道这个联动。

**格式**：JSONL，一行一条 JSON，`json.dumps(record, ensure_ascii=False)`
追加写入（`bridge/tools.py:67-75`）。

**⚠️ 关键发现：有两个写入者，schema 不一致。**

**写入者 A —— webui**（`webui/server.py:456-469`）：

```python
write_audit(
    audit_config,
    {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "webui",
        "agent": spec_id,
        "exit_code": result.exit_code,
        "ok": bool(result.ok),
        "needs_login": bool(result.needs_login),
        "timed_out": bool(result.timed_out),
        "duration_ms": result.duration_ms,
        "task": task[:120],
    },
)
```

**写入者 B —— 插件入口**（`__init__.py:691-702`）：

```python
self._audit(
    config,
    {
        "agent": spec.id,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "needs_login": result.needs_login,
        "duration_ms": result.duration_ms,
        "cwd": workdir or "",
        "task": task_text[:300],
    },
)
```

**差异对照**：

| 字段 | webui | 插件 | 说明 |
| --- | --- | --- | --- |
| `ts` | ✅ 有 | ❌ **无** | 插件路径的时间戳**丢失** |
| `source` | ✅ `"webui"` | ❌ **无** | 无法区分来源 |
| `ok` | ✅ 有 | ❌ **无** | 插件路径要靠 exit_code 推导 |
| `cwd` | ❌ 无 | ✅ 有 | 工作目录**只在插件路径有** |
| `task` | 截 120 字 | 截 300 字 | 长度不同 |
| 其余 | 一致 | 一致 | |

**还有第三个问题：插件路径默认一条都不写。**
`BridgeConfig.audit_log` 默认是 `""`（`bridge/config.py:67`），
而 `write_audit()` 第一行就是 `if not config.audit_log: return`（`bridge/tools.py:68-69`）。
只有 webui 路径显式兜底（`audit_config.audit_log = audit_config.audit_log or str(AUDIT_PATH)`），
**插件路径不会**。

⇒ **只有从 webui 控制台派发的任务才有记录；通过宿主（LLM 调用 plugin entry）
派发的任务，默认不留痕。**

前端正是被这个坑逼出来的（`index.html:723`）：

```javascript
const ok = r.ok === true || (r.exit_code === 0 && !r.needs_login && !r.timed_out);
const stamp = r.ts || r.at || "";
```

**读取侧**（`webui/server.py:133-145`）：`audit_tail()` 读全文、
`splitlines()[-limit:]` 取尾部、**倒序**返回，逐行 `json.loads`，
坏行直接跳过。

### 5.2 HTTP API 全表

`webui/server.py` 的 `Handler`（行 487 起），纯标准库
`ThreadingHTTPServer` + `BaseHTTPRequestHandler`。

**GET**：

| 路径 | 入参 | 返回 | 行 |
| --- | --- | --- | --- |
| `/` `/index.html` | — | 单文件前端 | 521 |
| `/api/health` | — | `{build, routes, features[]}` | 528 |
| `/api/agents` | — | `{agents[], count}` | 540 |
| `/api/version` | `agent`, `timeout`(3–45), `refresh` | `{agent, version, cached, note, seconds}` | 543 |
| `/api/harness` | — | `{harness[]}` | 586 |
| `/api/audit` | `limit`（≤200） | `{records[]}` | 589 |

**POST**：

| 路径 | 请求体 | 行 |
| --- | --- | --- |
| `/api/dispatch` | `{agent, task, cwd?, timeout_seconds?}` | 599 |
| `/api/enabled` | `{agent, enabled}` | 608 |
| `/api/install` | `{id, kind, dry_run?}` | 611 |
| `/api/install_skill` | `{id, dry_run?}` | 620 |
| `/api/locate` | `{id, path, kind}` | 628 |

**四个必须知道的性质**：

1. **只绑 127.0.0.1**（`server.py:653-655`）：

```python
if args.host not in ("127.0.0.1", "localhost"):
    print("只允许绑 127.0.0.1：这个面板能真派发任务，别暴露到网络上。", file=sys.stderr)
    return 2
```

2. **无鉴权、无 CSRF、无 CORS 头**。安全边界**完全**依赖「只绑回环地址」。
   ⇒ **本机上任何进程都能调 `/api/dispatch` 派发任意任务。**
   哨兵若要连它，等于把「本机任意进程可派发任务」这件事扩大。

3. **`/api/dispatch` 是同步阻塞的**（`server.py:438-447`）：

```python
result = asyncio.run(
    run_agent(spec, resolved, task, config=config, cwd=..., model=..., timeout=seconds, extra_args=...)
)
```

`asyncio.run` 在请求线程里**跑到任务结束或超时**为止。
默认超时 `config.default_timeout_seconds = 600`，上限 `max_timeout_seconds = 3600`。
**HTTP 连接全程挂着，中途没有任何进度可观测。**

4. **没有流式输出**（`server.py:20-21` 的自述）：

> 为什么不做流式输出：派发已经是一次请求-响应，SSE 只会多一层要维护的协议；
> 页面用计时器 + 一次性返回就够判断「在跑 / 跑完了」。

另外 `server.py:74-99` 的 `_warm_versions()` 用后台线程 + 3 并发预热版本，
`_VERSION_CACHE` TTL 600 秒 —— 这个「服务端预热」的做法哨兵的
v4.0.1 已经在雷达侧用过了（`src/gui/server.py` 的 `_warm_cloud_process_scan`）。

### 5.3 三个候选通道对比

| 维度 | A. 改它的 HTTP 接口 | B. 监听 `dispatch.jsonl` | C. 扫进程树 |
| --- | --- | --- | --- |
| 侵入性 | **要改上游代码**（违反本次红线） | **零侵入**（只读） | 零侵入 |
| 实时性 | 改好后可流式；现状是**阻塞到结束** | **无** —— 只在任务**结束后**才有一条 | **有** —— 但只有进程身份，没有输出 |
| 能否拿到 stdout | 现状：能，但要等结束 | **不能**（审计记录里没有输出） | **不能** |
| 能否拿到 PID / cwd | PID 不能；cwd 插件路径有、webui 路径没有 | 同左 | **都能**（PID + cwd） |
| 数据完整度 | 完整 | 只有账本字段 | 进程元数据 |
| 覆盖面 | webui 派发 | **仅 webui 派发**（插件路径默认不写） | 全部 |
| 哨兵现状 | 未接 | 未接 | **已经做了**（`src/proc_tree.py`） |

### 5.4 结论：最轻量的接管方式

**结论：哨兵应当「按上游的 AgentSpec 自己拉起 Agent 并接管管道」，
而不是去改上游的 HTTP，也不是去监听它的日志。**

理由（按重要性排序）：

1. **上游的 HTTP 是黑盒**。`/api/dispatch` 同步阻塞到任务结束，
   中途没有进度、没有 PID、没有流。哨兵的核心能力
   （L1 实时红线、L2 流式漂移）**必须**逐行拿到 stdout。
   要拿到流，就得改上游 —— 那是**侵入 + 破坏红线 + 破坏上游可维护性**。

2. **`dispatch.jsonl` 是事后账本，不是实时通道**。
   - 只在任务**结束后**落一条，进行中的任务**完全没有记录**；
   - 记录里**没有 stdout、没有 stderr、没有 PID**；
   - 两个写入者 schema 不一致（插件侧还缺 `ts`/`source`/`ok`）；
   - 插件路径**默认根本不写**（`audit_log` 为空直接 return）。
   ⇒ 用它做「事后审计归档」可以，做「实时接管」不可能。

3. **扫进程树拿不到「它在说什么」**。哨兵 `src/proc_tree.py` 已经在扫
   （v4.0.0 引入，v4.0.1 加了预热 + 1.5 秒熔断，冷启 3113ms → 70ms），
   它解决的是「**谁在跑**」，不是「**它说了什么**」。
   要拿输出，只能自己 `Popen` 拉起来 —— 那就是下面的方案。

4. **复用成本极低**。上游的 `AgentSpec` 是纯数据、`bridge/` 零依赖，
   把 14 条配置转成 JSON 喂给哨兵已有的 `AgentProfile` 机制，
   是本次盘点里**最划算**的一笔（§4.6）。

**但要分阶段做，不要一步到位：**

| 阶段 | 手段 | 侵入性 | 能拿到什么 | 建议 |
| --- | --- | --- | --- | --- |
| **阶段 1** | 哨兵 GET `/api/agents` + `/api/version` + `/api/health` | 零 | Agent 清单、启动形态、版本、可执行路径 | ✅ **推荐先做**。用它把哨兵雷达里「这个进程到底是哪个 Agent、怎么启动的」补全 |
| **阶段 2** | 哨兵 tail `dispatch.jsonl` | 零 | 上游的派发**结果**账本 | 🟡 可选。需先确认 `audit_log` 配置；注意 schema 不一致 |
| **阶段 3** | 哨兵按 AgentSpec **自己拉起** Agent，接管 stdout 管道 | 零（数据只读） | **实时 stdout + PID + cwd + 退出码** | ✅ **真接管必须走这条**。与哨兵现有 `--agent mcode/claude/codex` 模式同构 |

**如果坚持「不改上游、又要复用它的派发能力」**，
唯一可行的形态是：**哨兵作为 HTTP 客户端调 `/api/dispatch`**，
拿最终结果做**任务级**审查（不是会话级）。
代价：单次请求挂到任务结束（最长 3600 秒）、
无法做 L1 实时红线、中途挂掉就全丢、
且本机任何进程都能这么做（安全边界并没有因为哨兵而变好）。

**⇒ 建议：阶段 3 为主线，阶段 1 为补充；阶段 2 与 `/api/dispatch` 代理都不推荐。**

---

## 6. 任务四：改造成本评估

### 6.1 需要改哪些文件

| # | 文件 | 改动性质 | 规模 |
| --- | --- | --- | --- |
| 1 | **新增** `src/agents/cloud_agents.json` | 14 条 AgentSpec 的 JSON 转换产物 | ~300 行 JSON，**0 行逻辑** |
| 2 | **新增** `src/agents/__init__.py` | 加载 + 校验（id 唯一、mode 合法、prompt_flag 必填）+ `build_argv()` | ~120 行 |
| 3 | `src/proc_tree.py` | 把 `CLOUD_AGENT_BINARIES` / `CLOUD_AGENT_LABELS` / `CLOUD_AGENT_ALIASES`（行 137–185）改成从 JSON 加载；**补齐缺失的 8 条别名** | ~60 行 |
| 4 | `_构建与发布/scripts/run_with_sentinel.py` | `AGENT_PROFILES`（行 369–397）加 14 条；共享一个由 `mode` 驱动的 argv 构造器 | ~200 行 |
| 5 | `src/adapters/`（**最大成本项**） | 8 个 JSON 适配器 + pi 事件流解析 + 5 个纯文本兜底 | **640–1200 行** |
| 6 | `tests/test_cloud_agent_profiles.py` | argv 拼装、别名归一、鉴权匹配、超时语义 | ~200 行 |

### 6.2 分阶段工作量

| 阶段 | 内容 | 估算 | 交付物 |
| --- | --- | --- | --- |
| **A. 数据层** | 文件 1 + 2 + 3 + 6 的一半 | **0.5 天** | 哨兵雷达能显示 14 个 Agent 的完整信息（含别名与鉴权提示） |
| **B. 派发层** | 文件 4 + 冒烟 | **1 天** | 哨兵能按 AgentSpec 拉起任一 Agent 并拿到 stdout（先不做解析） |
| **C. 解析层** | 8 个 JSON 适配器 + L2 漂移接入 | **2–3 天** | 8 个 Agent 具备完整会话级审查 |
| **D. 兜底层** | pi 事件流解析 + 5 个纯文本 Agent 的启发式解析 | **1–2 天**（**能力打折**） | 剩余 6 个 Agent 至少能做「整段输出事后判定」 |
| **E. 真机校准** | 14 个 CLI 逐个实测（登录态、超时、退出码、输出形态） | **6–7 小时** | 校准后的参数与实测记录 |

**总计：4–6 个工作日 + 6–7 小时真机校准。**

> 阶段 E **不能省**。上游的 `notes` / `hints` 里的所有实测结论都是
> **作者那台机器**上得到的，换机器后未必成立（见 §6.4）。

### 6.3 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| **6 个 Agent 无可直接解析的输出格式**（pi 事件流 + 5 个纯文本） | L2 流式漂移做不了，只能事后判定 | 阶段 C/D 分开排期，明确告知能力边界 |
| **`pi` 的 `--mode json` 是事件流** | 解析难度显著高于其他 7 个 JSON Agent | 排在阶段 C 最后 |
| **各 CLI 的 JSON 形态互不相同** | 8 个适配器，无通用解 | 每个适配器单独写 + 单独测 |
| **上游的 claude 登记落后于哨兵现有实现** | 照抄会把 `--output-format stream-json` 降级成 `json` | **以哨兵现有的 `src/adapters/claude.py` 为准**，不照抄上游 |
| **鉴权元数据只有 5/14 完整** | 漏抄全局兜底表 → 未登录被误判成成功 | 两层都要实现（§4.4） |
| `failure_mode` 运行时无效 | step 的「静默挂起」语义无人实现 | 哨兵自己加一档「超时判定」 |
| 两份 provider 副本漂移 | 转换数据出错 | 只读 `providers/`，并加一条校验测试 |
| **上游的实测结论跨机失效** | 见 §6.4 | 阶段 E 逐条重测 |

### 6.4 哪些东西**不能照抄**（必须重测）

以下全部来自上游源码里的 `notes` / `hints`，是**作者那台机器**的观察：

| 陈述 | 为什么不能照抄 |
| --- | --- |
| `claude` / `pi` 启动时命中 `reg.exe` 程序黑名单 | 那是**作者的沙箱环境**产物，其他机器未必有 |
| `step` 未登录时静默挂起 40s+ 零输出 | 已登录的机器上根本不会挂起 |
| `hermes` 装在 `~/AppData/Local/hermes/` | 作者机的绝对路径 |
| `step` 在 `~/.stepcode/bin/step.exe` | 同上 |
| `codebuddy` 凭据挂在 WorkBuddy 桌面端 | 作者的安装形态 |
| `dsh` 走 `--profile headless` | 作者机上的 `dsh` 版本行为 |
| `evox` 需要 `--evox-cli` 才进 CLI 模式 | 作者机上的 `evox` 版本行为 |
| `command-code` `--max-turns` 默认 100、命中退出码 8 | 作者机上的 `cmdc` 版本行为 |
| `gemini` 未登录退出码 41 | 作者机上的 `gemini` 版本行为 |
| `qwenpaw` 是 `.cmd` 包装器、`evox` 是 `.cmd` 包装器 | 取决于本机安装方式（bun/npm/发行包） |

⇒ **`mode` / `fixed_args` / `prompt_flag` / `json_args` 这四个字段是「接口契约」，
相对稳定；`notes` / `hints` 里的行为描述是「版本相关事实」，必须重测。**

---

## 7. 最具复用价值的 3 个模块

### ① `bridge/registry.py` 的 `AgentSpec` 数据模型 —— **最值钱**

**为什么**：它把「怎么启动一个 Agent CLI」这件事**彻底数据化**了。
18 个字段覆盖了：可执行名、启动形态（`mode`）、参数模板、
JSON 输出开关、模型开关、别名、**鉴权失败措辞**、**非鉴权失败措辞**、
登录指引、安装命令。

**为什么对哨兵特别值钱**：哨兵现在只有 3 个 Agent
（mcode/claude/codex，硬编码在 `run_with_sentinel.py:369-397`），
每加一个 CLI 就要改 Python 源码。换成这套数据模型后，
**加一个 Agent = 放一条 JSON，0 行 Python**。

**可以直接抄的**：`AgentSpec` 的字段划分、`build_argv()` 的拼装顺序、
`redact_argv()` 的日志脱敏（把任务正文替换成 `<task>`）。

**哨兵侧落点**：`src/agents/cloud_agents.json` + `src/agents/__init__.py`。

### ② `bridge/provider_manifest.py` 的数据化注册协议 —— **最可复制**

**为什么**：这是**「零依赖、可 vendor、坏清单不拖垮整体」的注册协议范例**：
- `schema = 1` 版本号，不兼容就跳过并告警；
- 清单可以是 `provider.toml` 或 `provider.json`（兼容 Python ≤ 3.10）；
- `id` 必须等于目录名（防重复）；
- `mode` 白名单校验；
- **坏清单只收集成告警，绝不让整个插件起不来**
  （作者注释：宿主里那样会连带 5 个入口一起消失）。

**为什么对哨兵特别值钱**：哨兵是**打包成 exe** 发给用户的。
一个用户手改坏了配置文件导致哨兵起不来，是最糟糕的失败模式。
这套「读坏配置就降级成默认值」的模式，哨兵的 `bridge/config.py:168-186`
（`load_config()`）已经用了一半，但**没有 schema 版本与逐字段校验**。
上游这套更严。

**可以直接抄的**：schema 版本号、`id` == 目录名的去重规则、
坏清单降级为告警而非崩溃。

### ③ `bridge/runner.py` 的子进程派发与鉴权识别 —— **最救命**

**为什么**：这里的四个坑**每一个都是「不报错、只在生产里挂死」的类型**，
而且作者都写了实测数据：

1. **stdin 必须 DEVNULL**（否则 Agent CLI 继承 MCP 的 JSON-RPC 流，两边抢读）。
2. **不能用 `asyncio` 的 `Process.wait()`** ——
   `wait()` 要等所有管道 EOF 才返回，而 Agent CLI 派生的孙进程一直握着管道写端，
   导致**超时机制完全失效**。实测：退出 0.5 秒的父进程 + 活 20 秒的孙进程，
   `wait()` 就是 20 秒才返回；改用阻塞式 `Popen.wait(timeout=)` 后 0.5 秒返回。
   → 哨兵如果用 asyncio 拉 Agent，**会踩同一个坑**。
3. **超时必须连进程树一起清**（Windows 上 `taskkill /F /T /PID`），
   只杀父进程会留下孤儿进程占着文件和端口。
4. **输出限幅但不能停止读取**（管道写满会让子进程永久阻塞）。
   → 实现是「`_PipeReader` 后台线程 + 固定宽限期 5 秒」，
   把「进程退出」与「输出读完」彻底解耦。

**再加上鉴权识别的两条硬门槛**（§4.4）：
只看 stderr 开头 400 字 + stderr ≤ 800 字 + stdout 无实质内容。
**这三条门槛让「未登录」不会被误判成「任务完成」** ——
这是哨兵 L2 判定最怕的假成功。

**哨兵侧落点**：`src/core/` 下新增派发层，或改造现有接管管道。

**为什么它排第三而不是第一**：因为它的**行为**部分不能照抄（依赖宿主环境），
但它的**坑**必须照抄。而前两个模块可以**几乎原样 vendor**。

---

## 8. 附录：证据与边界

### 8.1 本次盘点的事实分级

| 级别 | 含义 | 本报告中的例子 |
| --- | --- | --- |
| **本次实测** | 本次盘点亲自观测到的 | 14 条 AgentSpec 的完整字段；16 条别名；前端 1034 行零依赖；`failure_mode` 运行时不被消费；两个审计写入者的 schema 差异；哨兵侧只有 8 条别名 |
| **代码推导** | 从源码逐行读出来的，未真机运行 | `dispatch.jsonl` 的记录结构；HTTP API 的 10 个接口与行为；argv 拼装规则 |
| **作者自述** | 上游源码注释里写的「实测」，**本机未验证** | 各 CLI 的退出码、挂起时长、鉴权措辞、安装路径 |
| **本文档推断** | 本报告的判断，**未经实现验证** | 改造成本估算；阶段划分；适配器数量估算 |

### 8.2 只读保证的核验

- 上游目录内 `__pycache__` 数量：**0**（用 `python -B` +
  `PYTHONDONTWRITEBYTECODE=1` 运行唯一的 import 后立即核验）；
- 本次唯一的上游侧执行是 `import bridge.registry`（只读 TOML，不发子进程）；
- 哨兵业务代码：**未改动**（见 §8.3）。

### 8.3 遗留问题

1. `webui/smoke.js`（5324 字节）本轮**未读** —— 它是前端冒烟测试，
   可能含额外的接口契约假设，抄前端前建议补读。
2. `skill/neko-bridge/frameworks.toml`（66 行 harness 矩阵）本轮只读了结构，
   未逐行核对 —— 它是「装机矩阵」，与哨兵审计业务无直接关系。
3. `ui/panel.tsx` 已读，但它是宿主内 React 面板，**不可复用**，未深入分析。
4. 上游自带的 `tests/` **未执行**（会真跑 CLI），
   因此作者声称的「29 passed, 1 skipped」本次未复核。

---

*本文档由只读代码盘点生成。所有行号引用指向盘点时的
`n.e.k.o_plugin_agent_bridge-main` 工作副本；上游项目为持续开发中的上游代码，
行号可能随上游更新漂移。*
