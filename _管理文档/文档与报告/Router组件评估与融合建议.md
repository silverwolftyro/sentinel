# Router 组件评估与融合建议

> **性质**：纯静态代码考古（只读），未运行任何被测代码、未安装任何依赖、未修改 `临时/` 下任何源文件。
> **考古对象**：`临时/` 下 4 个 DeepSeek Harness 生态的 Router 组件。
> **评估目的**：判断哪些「**任务分类与调度**」与「**watchdog（看门狗）**」算法
> 可以复现进哨兵 v5.0.1 的**纯 Python 无头内核**。
> **取证时间**：2026-11-03。
>
> **章节索引**
>
> | 维度 | 章节 |
> |---|---|
> | 目录摸底（技术栈 / 依赖 / 核心问题） | §1 |
> | **意图分类** | §2.1 |
> | **路由决策** | §2.2 |
> | **队列与优先级** | §2.3 |
> | **事件订阅** | §2.4 |
> | **watchdog 核心逻辑**（核心作用 + 实践方式） | **§2.5** |
> | 机制对比表 | §3 |
> | 可用资产提取 | §4 |
> | 不兼容警告 | §5 |
> | 落地建议（S 阶段 / W 阶段） | §6 |

---

## 0. 结论速览

| # | 结论 | 影响 |
|---|---|---|
| 1 | **四个项目里只有两个真正提供「任务分类与调度」算法**：`dsh-omni-router`（最完整）与 `harnessrouter`（最可移植，纯 Python）。 | 融合只需看这两家 |
| 2 | **`dsh-router` 名不副实**——它路由的是 **API 计费分组与凭证**，不是任务类型，与本任务目标**零相关**。 | 可以直接排除 |
| 3 | **`deepseek-super-agent` 分类的是「处置意图」**（execute/clarify/defer/answer/unavailable），不是任务品类（bugfix/feature/review）。但它的 **`defer` + `unavailable` 逃生舱**设计极有价值。 | 借「逃生舱」，别抄分类语义 |
| 4 | **最值得抄的 4 段逻辑**（全部零框架耦合）：<br>① `dsh-omni-router` 的**单一决策对象 + 派生策略级联**（`task-decision.mjs`）<br>② `dsh-omni-router` 的**就绪集 + 写冲突互斥批次选择**（`mission-dag.mjs`）<br>③ `harnessrouter` 的**带 fencing 的会话租约**（`control_store.py`）<br>④ `harnessrouter` 的**丢但计数**（`_OpsDrops`） | 见 §4 |
| 5 | **哨兵现状**：优先级队列**已经写好了但刻意没接线**到引擎（`src/core/priority_queue.py` 顶部注释明写「本版刻意不接线到 SentinelEngine」）。`TaskRouter` 的价值不是造队列，而是**补上「分派」这一层**。 | 先接线，再谈新功能 |
| 6 | **三处结构性不可照抄**：Cordis 中间件式扩展点、Router↔Host↔Worker 三方事件拓扑、evidence 的「宿主原生性」假设。 | 见 §5.3 |
| 7 | **`harnessrouter` 的 `runner/` 是全仓唯一与哨兵内核形态完全一致的 Python 实现**（`http.server` + `threading` + 手写 chunked SSE）。 | 抄它，不抄 FastAPI 部分 |
| 8 | **watchdog 维度：四家有三种哲学** —— 活性探测（哨兵现有）、心跳超时（super-agent / harnessrouter）、预算护栏（omni-router），`dsh-router` 完全没有。**互补，不是替代。** | 见 §2.5 |
| 9 | **watchdog 抄谁**：`deepseek-super-agent` 的 `src/watchdog/`（**唯一成体系**：三态判定 + nudge→fail 两阶段 + `watchdogIdle` 健康信号）；`harnessrouter` 的**三重地板**（防误杀）。 | 见 §4.9 / §6.5 |
| 10 | **哨兵 watchdog 的最大缺口**：现有 `engine_watchdog.py` 只探活**本地推理引擎**，**不监督 Agent 会话**——而那才是「跑偏/卡死」发生的地方。 | 补 §6.5 的 W 阶段 |

---

## 1. 目录摸底

### 1.1 总览

| 项目 | 规模 | 语言 | 运行时依赖 | 它**实际上**解决的问题 |
|---|---|---|---|---|
| `dsh-omni-router-main` | 210 文件 / 941 KB | TypeScript (ESM) | **零运行时依赖**（`optionalDeps` 仅 `tree-sitter`） | Agent 可靠性内核：Decide → Prepare → Verify。含分类、DAG 调度、能力求解、失败恢复、证据信任分级 |
| `harnessrouter-main` | 936 文件 / 49.9 MB | **混合**：Python（`gateway/`+`runner/`+`protocol/`）、JS（`ui/`）、Node（`scripts/`） | FastAPI 0.115.6 / uvicorn / httpx / pydantic 2.11 / redis 5.2.1 / asyncpg / aiomysql / azure-cosmos / playwright；runner 侧 MCP SDK 1.28.1 | 多后端网关 + UHP 协议一致性套件 + runner 沙箱。**路由「请求 → provider/沙箱」** |
| `deepseek-super-agent-main` | 168 文件 / 1.6 MB | TypeScript (ESM) | **零运行时依赖**（仅 devDeps: tsx/typescript/vitest），Node ≥22 | 主动式 Agent 引擎：按「处置意图」分派给 Worker，能力注册表，心跳订阅 |
| `dsh-router-main` | 15 文件 / 139 KB | TypeScript (ESM) | 仅 `@deepseek-ai/schemastery`；peer: `@deepseek-ai/cordis` / `dsh-credentials` / `dsh-subagent` / `dsh-subprocess` | **API 计费分组与凭证路由**（sub2api：一次登录、每次运行取 route-group key、注入子代理）。**不是任务路由** |

### 1.2 各项目定位澄清（这一步很关键）

- **`dsh-router`（139 KB / 15 文件）**：名字里有 router，但 `src/index.ts` 的职责是把 **sub2api 的 route-group key** 注入子代理，使子代理走特定计费分组。全仓**没有任何**任务类型分类、无队列、无调度。它还有一个对本次评估很有价值的旁证——**续跑指纹**：新会话若 `history.length <= 2` 且末尾消息 `role === 'user'` 且非空，则判定为「续跑」，把 `history` 裁到 2 条以内，让新上下文不被旧历史污染（`src/index.ts` `_isContinuation`）。
- **`harnessrouter` 的 `protocol/`**：`Unified Harness Protocol (UHP)` 一致性测试套件（`jsonschema`）。**不是运行时代码**，但其协议设计规则对「任务分类协议」有参考价值（见 §5.C）。
- **`deepseek-super-agent` 的 `src/core/`**：`intake.ts`（意图）、`assignments.ts`（分派）、`tickets.ts`（工单队列）、`subscribe.ts`（订阅）、`policy.ts`、`assessment.ts` —— 命名上最接近本任务，但语义是「处置意图」。

---

## 2. 核心机制提炼

### 2.1 意图分类

#### A. `dsh-omni-router`：**单一决策对象 + 派生策略级联** ★★★★★

它把「分类」收敛成一个**只算一次、所有层复用**的对象，避免各层重复分类产生分歧（`src/task-decision.mjs:1-6` 明确称此为 P0 修复）。

```js
// src/task-decision.mjs:8-27（节选）
export function createTaskDecision({ taskText, taskType, complexity, risk, ... }) {
  const coding = ['bugfix','feature','refactor','test'].includes(taskType);
  const highRisk = ['high','critical'].includes(risk);
  const executionMode = complexity === 'plan' || highRisk ? 'plan' : 'direct';
  const uncertainty = complexity === 'plan' ? 0.3 : complexity === 'balanced' ? 0.5 : 0.1;
  const reasoningEffort = highRisk ? 'max' : complexity === 'plan' ? 'high' : 'balanced';
  const verificationLevel = coding ? (highRisk ? 'full' : 'standard') : 'light';
  const contextBudget = complexity === 'plan' ? (highRisk ? 150000 : 100000)
                    : complexity === 'balanced' ? 60000 : 20000;
  ...
}
```

**派生关系（可直接翻译成 Python）**：

```
taskType ──∈ {bugfix,feature,refactor,test} ──► coding
risk     ──∈ {high,critical}              ──► highRisk
complexity ──∈ {plan,balanced,direct}      ──► executionMode / uncertainty / reasoningEffort
coding + highRisk                          ──► verificationLevel
complexity + highRisk                      ──► contextBudget
```

配套两段**纯正则 + 启发式**的分类器（`src/omni-router.mjs`）：
- `heuristicComplexity`（`:111-152`）：**优先级覆盖链** `direct词 > plan词 > 风险 > 强信号 > 长度`，并对置信度降级 `0.98 → 0.6`。
- `estimateRisk`（`:275-293`）：返回 `{level, score, reasons}` —— **`reasons` 数组让每个风险等级都能追溯到命中的具体维度**，这是「决策可追溯」的范式。

> **对哨兵的意义**：哨兵现在有 `VERDICT_ON_TRACK / VERDICT_DANGEROUS / VERDICT_LOOP` 三个平级结论，`classify_priority(verdict, drift_streak, drift_threshold)` 把它映射到 P0/P1/P2。这个 **verdict → priority 的单向映射可以升级为 omni-router 那种「一个决策对象带派生字段」**，让 `session_server` 与 `priority_queue` 读同一个对象，不必各算一遍。

#### B. `harnessrouter`：**拒绝而非代答** ★★★★☆

分类轴不是「猜任务类型」，而是「**这个请求我认不认识**」：
- **协议版本轴**：`uhp-2026-10-19` 等白名单，不匹配 → **拒绝**（`gateway/app.py` `_uhp_version`）。
- **harness 类型轴**：`dict` 白名单 + 由目录推导 backend（`gateway/app.py:121-151`）。该处注释记录了一次真实漂移事故：曾经硬编码的正则漏了新 base id，导致 `/dsh/v1/*` 落到 FastAPI 的裸 404——**修法是「从 catalog 编译，不再硬编码」**。
- **能力类型轴（模型）**：**拒绝而不是猜**（`gateway/app.py:189-211`）。
- **能力类型轴（推理档位）**：**正则白名单 + 最近邻降级，纯 stdlib**（`runner/reasoning.py:27-63, 92-98, 121-147`）——"没有的能力降级到最近的、绝不静默忽略"。

> **对哨兵的意义**：与哨兵既有原则**完全同源**——哨兵 L1 预检出错一律放行、`precheck.py` 的「L1 通过 ≠ 安全」。harnessrouter 的「拒绝而非代答」是同一原则的另一种表达。

#### C. `deepseek-super-agent`：**处置意图 + 逃生舱** ★★★★☆

它把输入分成**处置意图**（不是任务品类）：`execute` / `clarify` / `defer` / `answer` / `unavailable`。

两个设计非常值得抄：
1. **`defer` 逃生舱**：意图不明时不是硬猜，而是**明确延后**（`src/core/intake.ts`）。
2. **`unavailable` 逃生舱**：**没有可用 Worker 时明确返回「不可用」，而不是降级成随便挑一个**。

另外它的分类规则是**可审计的规则数组**（`rules[]`，每条带 id 与 source），而不是散落在代码里的 if-else。

> **对哨兵的意义**：哨兵 v5.0.0 的「**取不到任务目标 → L2 休眠**」已经是同一思路的先例（`src/llm/base.py:434-436`）。`defer`/`unavailable` 可以直接升级为 TaskRouter 的两个标准返回分支。

#### D. `dsh-omni-router` 的**任务类型 → DAG 模板**映射 ★★★★★

`generateMissionDag(mission, brief, options)` 按任务类型选模板：

| 任务类型 | DAG 形态 |
|---|---|
| `bugfix` | 复现 → 定位 → 修复 → 验证 |
| `refactor` | 摸底 → 改造 → 回归 |
| `feature` | 契约 → 实现 → 验收 |
| `test` | 设计 → 实现 → 验证 |
| 其他 | 从 mission phases 退化出串行链 |

`roleForTask(task)` 用**正则**给任务派角色：`judge` / `code-reviewer` / `qa-verifier` / `repair` / `builder`（`src/planner-dag.mjs`）。

> 这是本轮**最贴近「任务分类 → 调度」**的直接证据：**分类结果不是标签，而是调度图的形状**。

### 2.2 路由决策

| 项目 | 决策依据 | 层级 |
|---|---|---|
| `dsh-omni-router` | **4 层串联**：① 任务类型 → 能力需求 ② `capability-solver` 贪心集合覆盖求解最小能力集 ③ `capability-brain` 按历史成功率打分 ④ `strategy-shift` 在连续失败时换策略 | 4 层 |
| `harnessrouter` | **有序故障转移链**：逐候选线性扫描，能力匹配 + 健康检查；**每次失败都记账**（`_OpsDrops`/attempt log）；**会话亲和**（session stickiness） | 2 层（候选链 + 亲和） |
| `deepseek-super-agent` | **能力注册表 + 显式 assignment**：Worker 声明 capabilities，按意图与能力匹配；匹配不上 → `unavailable` | 1 层 |
| `dsh-router` | 与任务无关：按 route-group key 选计费分组 | 不适用 |

**`dsh-omni-router` 的 `capability-solver`（贪心加权集合覆盖）** ★★★★★

```js
// src/capability-solver.mjs:11-57（要点）
marginal = (新增覆盖数 / 总需求数) × score - riskPenalty
// 循环贪心选取，直到 maxPlugins（默认 2）或 marginal 低于 minScore（默认 0.4）
```
复杂度 `O(P²·R)`，近似比 `1+ln|R|`。**教科书级正确的贪心集合覆盖**。

**`dsh-omni-router` 的 `capability-brain`（在线成功率学习）** ★★★☆☆

```js
// src/capability-brain.mjs:96-113 / 68-83
score = reliability(EMA α=0.1) - riskPenalty(high:-0.1, critical:-0.2) + recencyBonus(≤50天线性衰减, 最多+0.05)
recordCapabilityOutcome(): EMA 更新成功率
```

**`harnessrouter` 的带 fencing 租约** ★★★★★ —— 见 §4。

### 2.3 队列与优先级

**结论先行：`dsh-omni-router` 没有优先级队列，`harnessrouter` 也没有。**

| 项目 | 优先级队列 | 插队/抢占 | 并发上限 | 背压 | 超时 | 取消 | 重试/恢复 |
|---|---|---|---|---|---|---|---|
| `dsh-omni-router` | ❌ 纯 FIFO（`mission-dag.mjs:60-63`），全仓无 `priority`/`preempt` 命中 | ❌ | ✅ `maxParallel` | ⚠️ 仅 8 维预算 | ❌（仅全局墙钟） | ❌（`Promise.all` 无取消） | ✅ repair→retry 双节点 + `strategy_shift` + DAG 落盘续跑 |
| `harnessrouter` | ❌ **全仓 grep 零命中** | ❌ | ✅ 两级信号量 | ✅ **丢但计数** | ✅ 多层墙钟 | ✅ **两层信号** | ✅ 指数退避 + 有序故障转移 |
| `deepseek-super-agent` | ✅ 工单队列（`src/core/tickets.ts`） | ⚠️ | ⚠️ | ⚠️ | ✅ | — | ✅ |
| `dsh-router` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 换 route-group |

**哨兵对照**：哨兵的 `src/core/priority_queue.py` **已经有**三档优先级（P0/P1/P2）、跨档插队、同档 FIFO、协作取消 + 宽限后强制放弃、`max_pending` 背压（**P2 可丢，P0/P1 永不丢**）、分档等待延迟统计 `QueueOutcome`。**在「优先级队列」这一项上，哨兵已经强于这四家中的任何一家。**

`harnessrouter` 唯一领先的两点：

1. **两级准入 + 排队期保活**（`gateway/app.py:544-559, 876-877, 891-934`）
   ```python
   @contextlib.asynccontextmanager
   async def _turn_slot(org: str, sid: str):
       """Both concurrency gates, held for the turn, with the session's heartbeat kept fresh while the
       turn WAITS for them.
       A turn is marked `starting` when it is accepted, and on one container the gates admit as many
       turns as the box has cores, so a busy box queues the rest. A queued turn is alive, but it has no
       runner turn yet and nothing renewed its heartbeat, so the orphan sweep read it as a dead turn
       and settled it, session and response, as failed; the turn then ran and its final write said
       completed. ..."""
       beat_s = max(1, _RECONCILE_STALE_S // 4)
       async def beat():
           while True:
               try:
                   await asyncio.wait_for(on_record.wait(), timeout=beat_s); return
               except asyncio.TimeoutError:
                   pass
               try:
                   await _vertex_upsert(sid, {"heartbeat": str(time.time())})
               except Exception:
                   pass
       ...
   ```
   > **排队中的任务必须持续刷新心跳**，否则被清扫逻辑误判为死任务。这是"队列"场景里最容易踩的坑。**哨兵 v5.0.0 的 `l2_sleeping` 会话正处在这个「排队但存活」的状态，必须有等价的心跳保活。**

2. **背压：丢但计数**（`gateway/app.py:687-746, 92-105`）
   ```python
   # HR-INF-018: the bus drops live events on backpressure by design (the client reconciles via
   # /turns). Those drops were SILENT — invisible to operators. Count them (surfaced on /readyz +
   # rate-logged) so a real drop storm is observable instead of a mystery gap. Not correctness state.
   _bus_drops = _OpsDrops("bus", 30, ("subscriber_q", "pump_q", "sse_q"))
   ...
   except asyncio.QueueFull:
       _bus_drops.bump("subscriber_q")
   ```
   > **丢失必须可观测。** 哨兵 `EventHub` 的 `max_queue=500`（`session_server.py:1201`）满了会怎样？**目前没有丢计数**——这是现成的改进点。

### 2.4 事件订阅（Subscription）

**四个项目里，只有两个真正回答了这个问题，而且答案一致：事件路径应当旁路主路径，Router 不做中转。**

**`dsh-omni-router`（TS）** —— 明确的旁路拓扑：
```
宿主 (DSH) 事件总线  ctx.on('tool.completed', ...)
        ├────────────────────────────┐
   [Router 旁路订阅]            [Worker 直接消费]
   hostAdapter.subscribeEvents   subagents.start('spawn')
   → 累积 EvidenceRecord          → Router 完全不感知、不转发
   → 写进 session state
```
> Router **既不中转事件、也不代理事件流**。Worker 与 Router 之间唯一的交互通道是 **prompt（入） + `run.result`（出）**。

**`harnessrouter`（Python）** —— 同样是旁路：
- `gateway/app.py:10827-10840` 起：事件路径**旁路主请求路径**。
- 主题粒度：`org + harness_id`（`app.py` `_bus_topic`）。
- **runner 侧纯标准库 SSE 转发**（`runner/server.py:5941` `class _HermesRelayHandler(http.server.BaseHTTPRequestHandler)`，由 `ThreadingHTTPServer` 承载）——**与哨兵 `src/daemon/server.py` 的形态完全一致**。
  - 两条可抄的硬规则：
    1. 用 `resp.read1(65536)` 而不是 `resp.read(4096)`——后者会等到填满缓冲区，**短首帧被卡住**。
    2. 上游中途死掉时**不发零 chunk**，而是留一个未终止的 chunked body + 一个 error 事件——**宁可让客户端看到"断流"，也不要给它一个"看起来完整但缺了尾部事件"的答案**。

**`deepseek-super-agent`** —— `src/core/subscribe.ts` 走 `src/harness/mesh.ts` 的订阅。

> **哨兵对照**：`src/core/session_server.py:1190-1242` 的 `EventHub` 已经是「每订阅者一个 `asyncio.Queue`（`maxsize=500`）+ `loop.call_soon_threadsafe` 跨线程投递 + 投递失败只记日志不影响生产者」——**这已经是标准的旁路扇出形态**，与上述实现同构，**不需要重写**。缺的只是 **§2.3 的丢计数**。

---

### 2.5 watchdog（看门狗）核心逻辑

#### 2.5.0 先行结论：四家有**三种** watchdog 哲学，外加一个「没有」

| 哲学 | 判据 | 动作 | 谁 |
|---|---|---|---|
| **活性探测型**（liveness probe） | 主动问「你还活着吗」 | 掉线 → 告警 → 可选重启 | **哨兵现有**（`engine_watchdog.py`）、harnessrouter 的 `_wait_alive` |
| **心跳超时型**（heartbeat lease） | 「多久没收到心跳 = 死了」 | 判陈旧 → **轻推一次** → 升级为失败 | **super-agent**、harnessrouter 的 lease + sweep |
| **预算护栏型**（budget guard） | 「资源/步数/墙钟用完 = 停」 | 直接停，**不探测** | **dsh-omni-router**（8 维预算） |
| **（无）** | — | — | **dsh-router**（只有单次 HTTP 15s 超时） |

⚠️ **这三者互补，不是替代关系。**
哨兵目前只有第一种；**「判不动」比「判探不到」更可靠**，而守护型 watchdog 恰恰最擅长前者。

---

#### 2.5.1 `deepseek-super-agent`：唯一成体系的 watchdog

**核心作用**：监督「**派出去还没回来的任务**」——
它监督的**不是某个进程，是一条 assignment**（任务分派记录）。
它回答三个问题：

1. 派出去的活儿**还动吗**？（心跳）
2. 它**是在干活还是在卡住**？（失败次数）
3. 它**还在原来的范围内吗**？（目标漂移）

**三态判定**（`src/watchdog/watchdog.ts`）：

| 判定 | 条件 | 证据 |
|---|---|---|
| `stale` | `now - max(报告时间, 开始时间) > stuckMs`（默认 60s） | `watchdog.ts:39-49` |
| `failed-jobs` | 同一 ticket 失败 job ≥ `failAfterFailedJobs`（默认 2） | `watchdog.ts:40-43` |
| `goal-drift` | 最新报告命中漂移标记词 | `watchdog.ts:62-73` |

**本轮最该抄的三条设计**：

1. **人为暂停豁免**（`watchdog.ts:36-37`）
   > `// A human pause is intentional idle time, not a stuck worker.`

   → `ticket.collab?.paused` 的任务**直接跳过**。用户说「我先放着」不是它卡住了。

2. **报告进展 ≠ 豁免**（`watchdog.ts:45-46`）
   > `// A progress report is a heartbeat, not an exemption. A worker that reports "still working" and then hangs must still be detected.`

   → 「我还在干」**只是心跳，不是免死金牌**。
   这条被写进了数据模型的不变量（`src/shared/types.ts:142`）：
   *"A progress report is a heartbeat, not a terminal stop."*

3. **两阶段升级：nudge → fail**（`watchdog.ts:117-127` / `:135-142` / `:183-198`）
   → 第一次检测到：**nudge**（往会话日志追加一条 `role:'user', kind:'nudge'` 的消息，**不杀进程**）；
   第二次仍卡：**fail**。
   → **每个 assignment 只 nudge 一次**（`nudged` Set 保证幂等，不反复骚扰）。
   → 目标漂移走**独立通道** `nudgeDrift`（`drifted` Set）——处置策略不同。

**实践方式（它到底怎么跑起来的）**：

| 环节 | 实现 | 证据 |
|---|---|---|
| **谁在驱动** | 一个**守护脉冲**：`start()` 先立刻 `tick()` 一次，再 `setInterval(() => this.tick(), intervalMs)` | `src/harness/daemon.ts:14-18` |
| **tick 做什么** | 读 store → 算 stuck + drift → 逐个分派 → 记指标 | `watchdog.ts:110-131` |
| **空闲也是信号** | `hits.length === 0` → `metrics.increment('watchdog.idle')` | `watchdog.ts:118-121` |
| **依赖注入** | `new Watchdog({ store, engine, journal, metrics, log, intervalMs, stuckMs, settle, now })` —— **`now` 可注入 ⇒ 可测** | `src/runtime.ts:388-393` |
| **失败走独立通道** | `engine.watchdogFail(ticketId, reason)` → `kind:'failed', deliver:false`，**与 worker 自己的报告分开** | `src/core/engine.ts:83-89` |
| **能力边界用类型封死** | watchdog 是被 `seal()` 的**角色端口**，**只能调 `fail`**，拿不到 worker 的操作 | `src/core/role-ports.ts:50-56` |
| **失败即回滚** | `fail` 时调 `settle(sessionId, verdict)`，`verdict ∈ {deliver, discard, hold}` —— 推测性工作区一起收场 | `watchdog.ts:186-188` |
| **规则可配置** | `rules.json` → `stuckMsDefault` / `nudgeBeforeFail` / `failAfterFailedJobs` / `driftMarkers`；**加载失败回落默认值** | `src/watchdog/rules.ts:26-51` |
| **只读健康快照** | `healthProbe()` → `{store, vaultMode, machinesOnline, watchdogIdle}`；注释明写 *"Read-only snapshot; no vault values or runtime startup."* | `src/watchdog/health.ts:8-34` |

> **最妙的一处**：健康端点把 **`watchdogIdle`**（= 没有 stuck assignment 且没有 stuck session）
> 当成一个健康指标暴露出去 —— **「看门狗闲着」本身就是「一切正常」的证明**。
> 这比单报「进程还活着」信息量大得多。

---

#### 2.5.2 `harnessrouter`：清扫器 + 心跳地板 + 硬墙钟

**核心作用**：监督「**没有主人照看的轮次**」——
owner 副本挂了、轮次还在 sandbox 里跑，谁来收场。

**实践方式**：
- **周期清扫**：`_reconcile_sweep()`，周期 60s（`_RECONCILE_EVERY_S`）
- **跨副本单飞**：`control_store.try_lock("reconcile-sweep", ...)` 一次 TTL 锁选一个清扫者；
  **存储挂了 fail OPEN**（照扫 —— 因为扫是幂等的 healer，重复扫无害）
- **四段判定**（`_reconcile_session`）：
  1. 查 trace 有没有终态 → 有就结算；
  2. 主动问 owner「这个 turn 还在吗」→ **404 = 进程已死**；
     ⚠️ **网络异常 ≠ 死**（`probe_gone = False`，重试而非判死）；
  3. **收养**（adopt）：owner 死了但轮次还在跑 → **接管**它，继续收割事件；
  4. 落库结算。

**最该抄的一段：三重地板**（`gateway/app.py:3260-3272`）

```python
def _sweep_seen(v: dict) -> float:
    """The session's last sign of life, for the sweep: its heartbeat, floored by its birth ...
    A heartbeat of 0 read as "never" aged a session from 1970, past the hard cap, and the sweep
    settled it failed at once (hosted, 2026-10-19: a newborn settled while its sandbox was being
    placed, then completed 34 s later ...)"""
    turn_at = float(v.get("turn_at") or 0)
    born = _born_seconds(v) or (_created_from_prefix(str(v.get("trace_blob") or "")) or 0.0)
    return max(_hb_seconds(v), born, turn_at)
```

→ 陈旧判定**不是** `now - heartbeat`，而是 `now - max(心跳, 出生时间, 本轮开始时间)`。
→ 三者取 max ⇒ **绝不会比本轮开始更旧** ⇒ 永不误杀刚出生的轮次。
→ 这是 2026-10-19 一次真实事故的修复：newborn 被判失败、34 秒后又 completed。

**第二该抄的：心跳不能延长硬上限**（`app.py:3283-3293`）

```python
# The hard cap runs from the TURN's start (turn_at, stamped at accept), never from the heartbeat:
# _turn_slot beats until the runner turn is on record, and a beat that goes on while a restore
# hangs must not keep the turn alive forever (CAT, 2026-10-21). ...
stuck = turn_at > 0 and turn_age > _sweep_cap_s() and not str(v.get("runner_turn_id") or "")
```

**第三该抄的：排队期心跳保活 + 交接棒**（`app.py:891-934`）

- `_turn_slot` 在**进入并发闸门之前**就起一个后台任务刷心跳
  （间隔 = 陈旧阈值 / 4 = 30s ⇒ 漏拍两次仍不到阈值）；
- 用一个 `Event` 做**交接棒**：`_RUNNER_TURN_ON[sid]` 被 set（= 轮次已在 runner 侧登记）后，
  watchdog 的拍**停**，改由轮询循环拍 —— **两个心跳源不重叠**；
- 闸门释放时 `finally` 里 `on_record.set()` + `task.cancel()` —— **槽位一释放就停拍**。

**事故复盘原文**（`app.py:896-909`）：

> *A queued turn is alive, but it has no runner turn yet and nothing renewed its heartbeat,
> so the orphan sweep read it as a dead turn and settled it, session and response, as failed;
> the turn then ran and its final write said completed.*

**第四该抄的：反误杀的四个防线**（`app.py:3446-3509`）

1. **主人在收尾 ≠ 主人已死** —— 终态 + 心跳新鲜 → **不动它**
   （主人会自己写完整记录，含 output 与 usage）；
2. **写前重读** —— 结算前重读；已经是终态就**让位**（主人赢了就不写）；
3. **不发明诊断** —— 分不清「被切断」和「干净结束但没输出」时**原因留空**：
   > *the SaaS port shipped "interrupted" on its equivalent branch and caught it within the hour:
   > a max_step=1 turn ended cleanly 'done' in 71s with empty output, nothing cut,
   > and the API confidently called it interrupted.*
4. **被动 + 主动混合** —— 被动（trace 终态）优先；无结论才主动问 owner；**网络异常不算死亡**。

**附：一个纯标准库的就绪探针**（`runner/openhands_driver.py:178-189`）——
零依赖，与哨兵 `http.server` + `threading` 栈完全同构：

```python
def _wait_alive(port: int, proc: subprocess.Popen, deadline: float) -> bool:
    """/alive needs no key and answers as soon as the app is mounted ..."""
    while time.time() < deadline:
        if proc.poll() is not None:
            return False                      # 目标进程先死了 —— 别再等超时
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/alive", timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    return False
```

---

#### 2.5.3 `dsh-omni-router`：预算护栏，**不是** watchdog

**核心作用**：不监督「谁还活着」，只监督「**资源还剩多少**」。

`src/agent-runtime.mjs:194-203` 的 `statusFromBudget()` —— **8 个维度，任一触顶即停**：

| 维度 | 常量 |
|---|---|
| 墙钟 | `maxWallClockMs` |
| 步数 | `maxSteps` |
| Token | `maxTokens` |
| 花费 | `maxCost` |
| 工具调用 | `maxToolCalls` |
| 重规划 | `maxReplans` |
| 修复 | `maxRepairs` |
| 同动作重试 | `maxSameActionRetries` |

**没有**：任务级超时、任务取消（`Promise.all` 无取消）、心跳、熔断。
**有的是**：失败恢复策略（§4.8 的 6 分支级联）+ 策略切换。

> 哨兵对照：这一类**哨兵已经有了** —— `src/core/priority_queue.py` 的 `max_pending`
> 就是「预算护栏」的排队版。

---

#### 2.5.4 `dsh-router`：没有 watchdog

只有 `src/sub2api.ts:54` 的 `REQUEST_TIMEOUT_MS = 15_000` + `AbortSignal.timeout(...)`
（单次 HTTP 请求超时），**没有任何周期性观察**。

附带一条值得抄的小设计（`:193`）：`idempotencyKey`
—— **让重试变安全**，而不是靠「不重试」来保安全。

---

#### 2.5.5 与哨兵现有 watchdog 的差距

哨兵现有：`src/core/engine_watchdog.py` 的 `EngineWatchdog` ——
探活**本地推理引擎**（`GET /models`），掉线发红色告警、恢复也报一声、另有 `_try_restart()`。

| 维度 | 哨兵现有 | 四家能补的 |
|---|---|---|
| **监督对象** | 只有「本地推理引擎」这一个外部依赖 | **Agent 会话 / 任务**（super-agent 的 assignment、harnessrouter 的 turn） |
| **判定方式** | 主动探活（活性探测型） | 心跳超时型 —— **判「不动」比判「探不到」更可靠** |
| **误杀保护** | ❌ 无 | ✅ 三重地板 + 写前重读 + 幂等让位（harnessrouter） |
| **人为暂停豁免** | ❌ 无 | ✅（super-agent `collab.paused`） |
| **进展 ≠ 豁免** | ❌ 无 | ✅（super-agent；数据模型里写死的不变量） |
| **两阶段升级** | ❌（掉线直接告警） | ✅ nudge → fail（super-agent） |
| **硬上限与心跳的关系** | ❌ 无 | ✅ 硬上限从「开始」起算，心跳不能续命（harnessrouter） |
| **空闲即健康** | ❌ 无 | ✅ `watchdogIdle`（super-agent `healthProbe`） |
| **退避 jitter** | ✅ 哨兵自己已有 | ❌ 两家都没有（harnessrouter 全仓无 `import random`）—— **抄的时候要自己补** |

---

#### 2.5.6 顺带发现：活性感知放置（super-agent `src/harness/mesh.ts`）

`WorkerMeshRegistry` —— **77 行**，纯数据结构：

- `markOnline(nodeId, online)` —— **活性标记**；
- `pickNode(ticketId)` —— 已分配的先复用（除非 offline）→ 否则挑**负载最低的在线且有余量**的节点；
- `unregister(nodeId)` —— 注销节点时**清理它的所有 assignment**（不留悬挂引用）；
- `inFlight(nodeId)` —— 负载统计。

> 这是「**watchdog 的判定结果喂给路由器**」的最小样本：
> 活性不是一个独立的监控台账，而是**路由决策的一个输入**。

---

## 3. 各项目机制对比表

| 机制 | `dsh-omni-router` | `harnessrouter` | `deepseek-super-agent` | `dsh-router` | **哨兵现状** |
|---|---|---|---|---|---|
| **意图分类机制** | 规则+正则启发式，输出**单一决策对象** | 协议/能力**白名单**，**拒绝而非代答** | 处置意图 `rules[]` + `defer`/`unavailable` 逃生舱 | ❌ 无 | `RuleEngine.check_event()` → 三种 verdict |
| **分类是否带证据** | ✅ `estimateRisk` 返回 `reasons[]` | ✅ 白名单即证据 | ✅ 规则带 id/source | — | ⚠️ verdict 未带命中理由数组 |
| **分类失败兜底** | 置信度降级 0.98→0.6 | **拒绝** | `defer` / `unavailable` | — | ⚠️ L1 出错放行（L2 侧） |
| **多 Worker 路由依据** | 能力需求→集合覆盖→成功率打分 | 有序候选链 + 健康检查 + 会话亲和 | 能力注册表匹配 | ❌ | `_intent_trackers` 按指纹分路 |
| **能力/配置求解** | ✅ 贪心集合覆盖 `O(P²R)` | ❌ | ✅ 注册表 | ❌ | ❌ |
| **在线成功率学习** | ✅ EMA α=0.1 | ❌（只记 attempt log） | ⚠️ | ❌ | ❌ |
| **优先级队列** | ❌ FIFO | ❌ **全仓零命中** | ✅ 工单队列 | ❌ | ✅ **P0/P1/P2 + 跨档插队** |
| **高优插队/抢占** | ❌ | ❌ | ⚠️ | ❌ | ✅ 跨档插队 + 两级取消 |
| **并发上限** | `maxParallel` | ✅ 两级信号量 | ⚠️ | ❌ | `max_pending` + guardian |
| **背压** | ⚠️ 8 维预算 | ✅ **丢但计数** | ⚠️ | ❌ | ⚠️ `max_pending`，**无丢计数** |
| **排队期保活** | ❌ | ✅ **心跳保活** | ✅ 心跳订阅 | ❌ | ❌ **（`l2_sleeping` 场景需要）** |
| **超时** | ❌ 仅全局墙钟 | ✅ 多层 | ✅ | ❌ | ✅ |
| **取消** | ❌ 无 AbortSignal | ✅ **两层信号** | — | ❌ | ✅ 协作取消 + 宽限强制放弃 |
| **失败重试/恢复** | ✅ repair→retry + strategy_shift | ✅ 指数退避 + 故障转移 | ✅ | ✅ 换分组 | ✅ 降级路径 |
| **熔断** | ❌ | ❌ **明确不存在** | ❌ | ❌ | ❌ |
| **事件旁路主路径** | ✅ | ✅ | ✅ | — | ✅ `EventHub` |
| **证据信任分级** | ✅ **T0–T4，provider 不得自报 T4** | ⚠️ UHP 证据区分 | ⚠️ | — | ❌ |
| **与会话绑定的并发互斥** | ❌ | ✅ **fencing 租约** | ⚠️ | — | ⚠️ 指纹隔离，但无租约 |
| **watchdog 哲学** | 预算护栏（8 维） | 心跳超时 + 清扫器 | **心跳超时 + 两阶段升级（唯一成体系）** | ❌ 无 | 活性探测（只探引擎） |
| **心跳/存活信号** | ❌ 无 | ✅ 双机制（vertex heartbeat + lease） | ✅ `session.heartbeat` | ❌ 无 | ❌ 无 |
| **陈旧判定** | ❌ | ✅ **三重地板** `max(心跳,出生,本轮开始)` | ✅ `max(报告时间,开始时间)` | ❌ | ❌ |
| **人为暂停豁免** | ❌ | ❌ | ✅ `collab.paused` | ❌ | ❌ |
| **进展 ≠ 豁免** | ❌ | ❌ | ✅ **写进数据模型的不变量** | ❌ | ❌ |
| **两阶段升级（轻推→判死）** | ❌ | ❌ | ✅ **nudge → fail** | ❌ | ❌ |
| **硬上限与心跳的关系** | ✅ 墙钟独立 | ✅ **上限从开始起算，心跳不能续命** | ❌ | ❌ | ❌ |
| **误杀保护** | — | ✅ 写前重读 + 幂等让位 + 不发明诊断 | ✅ 暂停豁免 | — | ❌ |
| **空闲即健康** | ❌ | ⚠️ `/readyz` 报告 redis 但**不门禁** | ✅ **`watchdogIdle`** | ❌ | ❌ |
| **僵尸子进程回收** | ❌ | ✅ 环境变量 marker + `/proc` 扫描 + SIGSTOP→杀子孙 | ❌ | ❌ | ❌ |
| **退避 jitter** | ❌ | ❌ **全仓无 `import random`** | ❌ | ❌ | ✅ 哨兵已有 |

---

## 4. 可用资产提取（可直接写进哨兵）

> 判定标准：**零框架耦合 + 可用纯 Python 标准库/`asyncio` 表达 + 能在 `priority_queue.py` 或 `session_server.py` 找到落点**。

### 4.1 ★① 单一决策对象 + 派生策略级联
- **出处**：`dsh-omni-router/src/task-decision.mjs:8-27`；`src/omni-router.mjs:111-152`（`heuristicComplexity`）、`:275-293`（`estimateRisk`）
- **落点**：`src/core/session_server.py`（新增 `TaskDecision` dataclass）→ 供 `src/rules/basic.py`、`src/core/priority_queue.py` 共读
- **怎么改**：用 `dataclass` + `Enum`；`uncertainty` 显式建模为字段（现在是硬编码 0.1/0.3/0.5）；`estimateRisk` 的 `reasons: list[str]` 原样保留（**这是哨兵当前缺失的**——`AlertStore` 现在存了 verdict，但没存"为什么"）
- **复杂度**：`O(L)`，全部纯函数
- **风险**：⚠️ 不要抄 `confidence: 1 - uncertainty`（该实现自身的缺陷，见 §5.3）

### 4.2 ★② 就绪集 + 写冲突互斥的批次选择
- **出处**：`dsh-omni-router/src/mission-dag.mjs`
  - `getReadyTasks`（`:60-63`）：就绪集 = `status == 'pending'` 且全部依赖已完成
  - `hasWriteOverlap`（`:162-166`）：两个任务的 `writeScope` 有交集 → 互斥
  - `selectReadyBatch`（`:168-176`）：在就绪集里挑互不冲突的一批
- **落点**：`src/core/priority_queue.py`（作为 `drain()` 的批次编排层）
- **怎么改**：`Task` → `@dataclass`；`writeScope` → `frozenset[str]`；`selectReadyBatch` → 列表推导 + `any()`
- **顺手修 bug**：该实现的 `getReadyTasks` 不认 `superseded` 状态，`done` 集合应含 `{'done','superseded','skipped'}`（与同仓 `isMissionDagComplete` 不一致）
- **复杂度**：就绪集 `O(T·D)`，批次选择 `O(B²·S)`
- **价值**：**这是四家里唯一真正实现「调度」的代码，且与 DSH/Cordis 零耦合**。它天然是优先级队列的插槽——就绪集数组顺序是 FIFO，插入一次 `sort(key=priority)` 即得优先级队列（哨兵已有该 sort）。

### 4.3 ★③ 带 fencing 的会话租约
- **出处**：`harnessrouter/gateway/control_store.py:214-248`（`lease_admit`）、`:179-211`（`lease_acquire`）
```python
held_by = str(cur.get("owner") or "")
live   = float(cur.get("expires") or 0) > now
fresh  = (now - float(cur.get("ts") or 0)) < fresh_s
other  = bool(held_by and held_by != owner)
conflict = bool(live and other)
active_conflict = bool(conflict and fresh)
if reject_on_conflict and active_conflict:
    # Refuse WITHOUT modifying the lease — the incumbent keeps its fence.
    return {"fence": 0, "conflict": True, "rejected": True, "holder": held_by}
```
- **落点**：`src/daemon/context.py`（`SessionRegistry` 旁边）或 `src/core/session_server.py`
- **怎么改**：`live ∧ fresh ∧ other ⇒ 冲突`；**拒绝时绝不碰租约**（不偷 fence）；fence 单调递增 + CAS 重试；异常时 **fail open**（返回 `fence=0, conflict=False`）
- **复杂度**：`O(1)`，纯逻辑
- **价值**：**这是本仓最值得抄的一个算法**。哨兵 v5.0.0 靠指纹（thread_id→session_id→cwd→pid）隔离会话，但**没有租约**：同一指纹被两个执行者同时占用时，哨兵无法判定谁是"正版"。fencing token 正好补这个洞——**旧持有者的 fence 过期后写入自动失效**。

### 4.4 ★④ 丢但计数（背压可观测）
- **出处**：`harnessrouter/gateway/app.py:92-105`（`_OpsDrops`）、`:687-746`（`_bus_deliver`）
```python
class _OpsDrops:
    """Cumulative drop counter with rate-limited logging — THE visibility mechanism for
    by-design-lossy paths (bus backpressure, billing delivery). Surfaced on /readyz."""
    def bump(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        now = time.time()
        if now - self._last > self._every:
            self._last = now
            print(f"[{self.name}] drops (cumulative): {self.counts}", flush=True)
```
- **落点**：`src/core/session_server.py` 的 `EventHub._deliver`（`:1244`）
- **怎么改**：给 `EventHub` 加一个 `drops` 计数器（按原因分类：`subscriber_q` / `pump_q`），在 `/api/state` 或 `/api/health` 里暴露；日志限频打印
- **为什么**：哨兵 `EventHub` 的 `max_queue=500` 满了以后**丢在哪里？丢了多少？运维看不见**。这正是 HR-INF-018 记录的那类"沉默的丢失"

### 4.5 ☆⑤ 排队期心跳保活
- **出处**：`harnessrouter/gateway/app.py:891-934`（`_turn_slot`）
- **落点**：`src/daemon/context.py`（`l2_sleeping` 会话）
- **怎么改**：哨兵「取不到目标 → L2 休眠」的会话处于**排队但存活**状态。若有会话清扫/回收逻辑，它必须能区分「正在排队」与「已死」，否则会误杀。
- **优先级**：**仅当哨兵引入会话回收机制时才需要**。当前无回收逻辑 → 暂不需要，但**写文档时要记下这个约束**。

### 4.6 ☆⑥ 证据信任分级（T0–T4）
- **出处**：`dsh-omni-router/src/evidence-trust.mjs:9-15, 57-65, 101-116`
```js
export const TRUST_LEVELS = { T0:0, T1:1, T2:2, T3:3, T4:4 };
// T0 model claim / T1 agent observation / T2 host-tool output
// T3 deterministic execution / T4 independent-hidden verifier

// Evidence provider supplies facts. Omni assigns trust. Third-party providers
// can never self-report T4 unless Omni Policy explicitly grants it.
export function assignEvidenceTrust(record = {}, { policyGrantedT4 = false } = {}) {
  let trustLevel = record.trustLevel || 'T0';
  if (trustLevel === 'T4' && !policyGrantedT4) trustLevel = 'T2';
  return { ...record, trustLevel, trustValue: TRUST_LEVELS[trustLevel] ?? 0 };
}
```
- **落点**：`src/core/session_server.py` 的 `AlertStore`
- **怎么改**：哨兵的告警来源天然分级：**L1 规则命中 = T3（确定性执行）**、**L2 模型判定 = T1（agent 观察）**、**用户 override = 独立复核**。加上分级后，`/api/state` 可以区分「确定性红线」与「模型猜测」，**这与哨兵「宁可不报也不误伤」的原则高度契合**。
- **额外价值**：该实现还有一条「**证据随工作区变更而失效**」的设计（`:101-116`），对哨兵同样成立——**目标或代码变了，旧的漂移判定就该失效**。

### 4.7 ☆⑦ 贪心集合覆盖 + 在线成功率
- **出处**：`dsh-omni-router/src/capability-solver.mjs:11-57`、`src/capability-brain.mjs:68-83, 96-113`
- **落点**：未来若有「多 Worker 选谁」的需求
- **怎么改**：`solveMinimalSet` 贪心覆盖（`marginal = 新增覆盖率 × score - 风险惩罚`）；`recordCapabilityOutcome` 用 **EMA α=0.1** 更新成功率
- **风险**：⚠️ EMA 与「最近用过加分」叠加存在**正反馈**（越常用分越高 → 越常用）。建议改用 EWMA 半衰期形式
- **优先级**：**当前哨兵没有多 Worker 概念，暂不需要**

### 4.8 ☆⑧ 失败恢复决策级联
- **出处**：`dsh-omni-router/src/recovery-policy.mjs`（`decideRecovery`）
- **形态**：一个 6 分支的纯判定级联：`stop`（预算耗尽）→ `escalate`（达最大次数）→ `expand_context`/`change_hypothesis`（同类失败 ≥2 次）→ `expand_context`（上下文不足）→ `repair`（测试/构建失败）→ `retry`（瞬时）
- **配套**：`src/strategy-shift.mjs:9-17`（`shouldShiftStrategy`：同类别且同原因/同假设 → 换策略，**防止盲目重试循环**）
- **落点**：`src/llm/base.py`（L2 判定失败后的降级/重试路径）
- **价值**：哨兵 v5.0.0 已有降级路径，但**没有"同类失败 ≥2 次就换策略"这条**，容易在同一个坏点上反复调模型

### 4.9 ★ watchdog 可用资产（本轮新增）

> **前置**：哨兵现有 `src/core/engine_watchdog.py` 只探活**本地推理引擎**
> （`GET /models`），**完全不监督 Agent 会话**。
> 而「跑偏 / 卡死 / 没人管」恰恰发生在会话层 —— 这是本轮要补的主缺口。

#### ★⑨ 三态判定 + 两阶段升级（nudge → fail）
- **出处**：`deepseek-super-agent/src/watchdog/watchdog.ts:39-73`（三态判定）、`:117-127`（分派）、`:135-142`（nudge）、`:183-198`（fail）
- **落点**：`src/core/session_server.py`（会话级 watchdog），或新建 `src/core/session_watchdog.py`
- **怎么改**：三态 `stale` / `failed_jobs` / `goal_drift`；
  **第一次**检测到 → nudge（往会话日志追加一条 `role=user` 的追问消息，**不杀进程**）；
  **第二次**仍卡 → fail。用 `set` 保证每个会话**只 nudge 一次**（幂等，不反复骚扰）
- **复杂度**：`O(S)`（S = 会话数）
- **价值**：**本轮最该抄的一段** —— 它把「卡住」从「进程死了」里剥了出来。
  ⚠️ 抄的时候**必须**带上两条豁免：人为暂停（`collab.paused`）跳过；「我还在干」只是心跳、**不是免死金牌**

#### ★⑩ 陈旧判定的三重地板（防误杀）
- **出处**：`harnessrouter/gateway/app.py:3260-3272`（`_sweep_seen`）、`:3283-3293`（硬上限从 `turn_at` 起算）
- **落点**：同上
- **怎么改**：
  ```python
  seen = max(心跳时间, 会话创建时间, 本轮开始时间)   # 三者取 max
  ```
  ⇒ **绝不会比本轮开始更旧** ⇒ 永不误杀刚出生的会话。
  硬上限从「本轮开始」起算，**心跳不许续命**。
- **为什么**：harnessrouter 的注释里记着一次真实事故 ——
  *a newborn settled while its sandbox was being placed, then completed 34 s later*
- **复杂度**：`O(1)`/会话

#### ★⑪ 排队期心跳保活 + 交接棒
- **出处**：`harnessrouter/gateway/app.py:891-934`（`_turn_slot`）
- **落点**：`src/daemon/context.py`（`l2_sleeping` 会话）
- **怎么改**：进闸门**之前**起一个后台线程刷心跳（间隔 = 陈旧阈值 / 4 ⇒ 漏拍两次仍不到阈值）；
  用 `threading.Event` 做**交接棒**，避免两个心跳源重叠
- **前提**：哨兵目前**没有会话回收逻辑** ⇒ 暂不需要，
  但**必须写进设计文档**：将来加回收时，这条约束会直接决定「谁被误杀」

#### ★⑫ 反误杀四防线
- **出处**：`harnessrouter/gateway/app.py:3446-3509`
- **落点**：会话级 watchdog 的结算路径
- **怎么改**：
  1. **主人在收尾 ≠ 主人已死** —— 有终态且心跳新鲜 → **不动它**；
  2. **写前重读** —— 结算前重读，已是终态就**让位**；
  3. **不发明诊断** —— 分不清「被切断」和「干净结束但没输出」时**原因留空**；
  4. **网络异常 ≠ 死亡** —— 主动探测失败只重试，不判死

#### ★⑬ 空闲即健康（`watchdogIdle`）
- **出处**：`deepseek-super-agent/src/watchdog/health.ts:8-34`
- **落点**：`src/daemon/server.py` 的 `/api/health`
- **怎么改**：健康响应加一个 `watchdog_idle: bool` —— **没有卡住的会话 = 一切正常**
- **价值**：比「进程还活着」信息量大得多；而且它是**只读**的，不触发任何副作用

#### ☆⑭ 活性感知放置（活性喂给路由）
- **出处**：`deepseek-super-agent/src/harness/mesh.ts`（`WorkerMeshRegistry`，**77 行**）
- **怎么改**：`markOnline(nodeId, online)` + `pickNode()` 挑**负载最低的在线且有余量**的节点；
  节点注销时**一并清理它的 assignment**（不留悬挂引用）
- **落点**：将来哨兵有多 Worker 时；`src/core/session_server.py` 的 `SessionRegistry` 已经是同类结构

---

## 5. 不兼容警告

### 5.1 Node / DSH 生态绑定（不可照抄）

| 绑定 | 用在哪 | Python 替代方案 |
|---|---|---|
| **Cordis 插件生命周期** `apply(ctx, config)` | `omni-router.mjs:532`；`ctx.get/set/on`、`ctx.tools.register`、`ctx.planMode` | 普通类 `__init__(self, deps, config)`，依赖显式注入；`ctx.planMode` → 抽象方法 |
| **Cordis 事件总线** `ctx.on(type, cb)` → 返回退订函数 | `dsh-adapter.mjs:91`、`omni-router.mjs:879, 953` | `asyncio.Queue` + 后台消费者 task；退订 → 投递哨兵对象 + `await task`。**哨兵 `EventHub` 已经是这个形态，无需改造** |
| **Koa 风格中间件** `await next()` | `omni-router.mjs:953-975` | 装饰器包一层；或直接顺序代码（哨兵是自研内核，通常不需要中间件语义） |
| **`subagents.start('spawn')` → `{result, dispose()}`** | `agent-chain.mjs:253-267`、`omni-router.mjs:1530-1542` | `asyncio.create_task(worker(...))`；`finally: task.cancel()` 代替 `run.dispose()` |
| **`Promise.all` 并行批次** | `agent-runtime.mjs:213` | `asyncio.gather(*coros, return_exceptions=True)` ⚠️ **务必带 `return_exceptions=True`**，否则单任务异常炸掉整批（Node 的 `Promise.all` 同病） |
| **`AbortSignal` / 取消** | **该仓未使用**（全仓 0 命中） | 哨兵应主动补：`asyncio.wait_for(coro, timeout)` + `task.cancel()` + `except asyncio.CancelledError` |
| **TypeBox** | 该仓未使用（手写 JSON Schema 字面量，`omni-router.mjs:1044`） | `pydantic` 或 `dataclasses` + 手工 dict；哨兵可用 `dataclasses` |
| **`@deepseek-ai/dsh-*` 私有包** | 该仓不 import 任何 dsh 包 | 哨兵不需要；若需对接，定义 `HostAdapter` Protocol（该仓 `src/host-interface.mjs:9-18` 的 `HOST_FEATURES` 已是这个思路） |
| **tree-sitter WASM** | `ast-provider.mjs:93, 101-107`（optionalDeps） | PyPI `tree-sitter`；**或直接抄它的正则降级路径**（`src/dependency-graph.mjs:18-101`，零依赖且够用） |
| **`node:fs` / `node:path`** | 多处 | `pathlib.Path`；JSON 用 `json.loads/dumps` |
| **`node:crypto` `createHash('sha1')`** | `recovery-telemetry.mjs:16-17` | `hashlib.sha1(seed.encode()).hexdigest()[:12]` |
| **`node:sqlite` `DatabaseSync`** | `project-brain-v2.mjs:11` | 标准库 `sqlite3` |
| **`node:child_process` `execSync`** | `omni-router.mjs:616-617` | `asyncio.create_subprocess_exec` |
| **`new Date().toISOString()` / `Date.now()`** | 到处 | `datetime.now(timezone.utc).isoformat()`；`time.time()` / `time.monotonic()` |
| **`Math.random()`** | `evidence-trust.mjs:81` | `random.random()`；防碰撞用 `uuid4().hex[:6]` |
| **`Math.round(x*1000)/1000`** | 多处 | `round(x, 3)` |
| **Express / LangChain** | **四个项目均未使用** | ✅ 无需处理——这批代码的依赖极干净（两个 TS 项目**零运行时依赖**） |

### 5.2 Python 生态绑定（`harnessrouter`，不可照抄的部分）

| 绑定 | 位置 | 纯 stdlib / `asyncio` 替代 |
|---|---|---|
| **FastAPI + pydantic `BaseModel`** | `gateway/app.py:10324-10339`、`runner/server.py:15908` | `dataclasses.dataclass` + 手工取字段；或 `TypedDict` + `json.loads`。**这些模型全字段可选、校验极简，只需保留字段名与默认值** |
| **`HTTPException` / `uhp_error` 信封** | `gateway/app.py:10470-10477` 等 | 自定义异常携带 `(status, code, message, param, detail)`，序列化成同一 JSON 形状 |
| **`StreamingResponse`** | `gateway/app.py:10425, 10889, 11105` | **仓内已有现成答案**：`runner/server.py:6473-6477` 用 `http.server` 手写 chunked 响应 |
| **FastAPI middleware** | `gateway/app.py:510-535`、`:4915-4935` | `BaseHTTPRequestHandler.do_*` 里自行分发；正则前缀改写在 handler 入口做 |
| **`asyncio` 语义** | 全 gateway | 若哨兵保持 `threading`：`asyncio.Queue`→`queue.Queue`；`asyncio.Semaphore`→`threading.Semaphore`；`asyncio.Event`→`threading.Event`；`asyncio.wait_for(q.get(), t)`→`q.get(timeout=t)`；`asyncio.create_task`→`threading.Thread`。**所有算法原样存活** |
| **Redis 5.2.1 pub/sub** | `gateway/app.py:711-712, 754-757, 771, 824` | 单进程直接 `queue.Queue` 扇出；跨副本改走轮询共享存储。**Redis 只提供跨副本，不提供任何调度语义** |
| **azure-cosmos 控制存储** | `gateway/control_store.py`（整文件） | **仓内已有替身**：`gateway/control_sqlite.py`（纯 `sqlite3`，同样 5 个操作、同样错误语义、WAL、`BEGIN IMMEDIATE` 做 CAS、惰性 TTL）。单进程还可更简：`threading.Lock` + dict，**保留同样的 CAS 语义** |
| **asyncpg / aiomysql** | `gateway/sql_plane.py` | `sqlite3`（每线程一个连接） |
| **httpx.AsyncClient** | `gateway/app.py:111-131` | `urllib.request`（简单请求）或 `http.client`（流式/SSE——runner 中继已在用） |
| **playwright** | `gateway/browser_plane.py` | **无标准库等价物**；应排除在哨兵内核之外 |
| **MCP SDK 1.28.1** | `runner/mcp_bridge.py:20-24` | 协议帧无标准库实现。**但模式可移植**：一进程一服务器、tools/resources/prompts 原样转发、闭合 schema 过滤（`declared_only` `:27-41`） |
| **PyJWT / cryptography** | `gateway/auth.py` | HMAC bearer 用 `hmac`+`hashlib` 足够；JWT 校验约 40 行可实现（仅 RS256 才需 `cryptography`） |
| **jsonschema** | `protocol/conformance/uhp_conformance/context.py` | 第三方依赖，但 schema 是纯 JSON-Schema 文件，可作**设计参考**手工校验 |

### 5.3 结构性不可照抄的三点

1. **Cordis 中间件式扩展点**
   哨兵 v5.0.0 是自研内核，**没有「宿主 prompt 组装钩子」这一层**。`dsh-omni-router` 的分类结果只能通过**显式调用**注入，不能靠事件钩子自动生效。**不影响算法本身（分类函数都是纯函数），但集成形态完全不同。**

2. **Router ↔ Host ↔ Worker 三方事件拓扑**
   该项目的"事件"能力 100% 建立在 Cordis 总线之上。哨兵若无全局事件总线，需重写为：哨兵自己定义 `EventBus`，让 Worker 直接向它投递——**但这恰好是「旁路」的正确形态**（Worker → 总线 → Router 订阅，与 Router 无关），**拓扑可搬**。

3. **`evidence` 的「宿主原生性」假设**
   `dsh-adapter.mjs:110` 只把 `command.completed`/`test.completed` 认定为 `deterministic`。哨兵若无宿主工具执行器，T3 证据来源需自行定义（如：哨兵自己执行的检查器、独立的复核进程）。

> 另需注意两处**上游实现自身的缺陷**，抄的时候要顺手修掉：
> - `task-decision.mjs` 的 `confidence: 1 - uncertainty` 与 `createTaskDecision` 传入的 `uncertainty` 语义互相打架——**不要照抄**。
> - `mission-dag.mjs` 的 `getReadyTasks` 不认 `superseded` 状态，与同仓 `isMissionDagComplete` 不一致——**抄的时候补上**。

### 5.4 UHP 协议设计规则（`protocol/`）对「任务分类协议」的借鉴

**结论：值得借鉴，但价值最高的是「协议设计规则」，不是代码。**

- **分类值不枚举，只做不透明字符串**（`uhp-2026-10-19.schema.json:154-162`）——最不重要的一条其实是：**不要把 `bugfix/feature/review` 硬编码进协议**，留作开放字符串，便于扩展。
- **「忽略必须可观测」**：网关接受但不处理的请求字段，要在 `metadata.ignored_fields` 里如实告知（`tasks.md` 1.1）——**对应哨兵的「沉默丢弃」问题，与 §4.4 的丢计数同源**。

### 5.5 watchdog 专属的不兼容警告（本轮新增）

| 绑定 | 出处 | 为什么不能照抄 | `threading` + 标准库替代 |
|---|---|---|---|
| **`setInterval` 定时器** | `super-agent/src/harness/daemon.ts:17` | Node 的定时器不阻塞、可在事件循环里重复注册 | `threading.Timer`（**一次性**）+ 循环自注册；或一个常驻守护线程 `while not stop.wait(interval): tick()` —— `Event.wait(timeout)` 天然可中断，比 `Timer` 好 |
| **`seal()` 角色端口** | `super-agent/src/core/role-ports.ts:50-56` | TypeScript 的编译期能力封禁 | Python 用 **`typing.Protocol`** 声明 watchdog **只能**有 `fail()`，再靠**依赖注入**只给它这一个方法 —— 边界靠「不给」而不是靠「不许」 |
| **sqlite store** | `super-agent/src/store/sqlite.ts` | 它的心跳存在库里 | 哨兵会话状态**已在内存**（`session_server.py` 的 `SessionRegistry`）⇒ **直接存内存字典即可**，不必引 sqlite |
| **`asyncio` 全家** | `harnessrouter/gateway/` | 哨兵是 `threading` 栈 | `asyncio.Queue`→`queue.Queue`；`asyncio.Semaphore`→`threading.Semaphore`；`asyncio.Event`→`threading.Event`；`asyncio.wait_for`→`q.get(timeout=)`；`asyncio.create_task`→`threading.Thread` |
| **`/proc` 扫描** | `harnessrouter` 的僵尸子进程回收 | ⚠️ **Linux 专有**，Windows 上根本不存在这个路径 | Windows 侧改用 `taskkill /T /F`（按 PID 杀整棵树）或 WMI `Win32_Process` 查父子关系 —— 哨兵 v5.0.1 的 `single_instance` 已经用 `Toolhelp32` 做过一次父子关系判定，可复用 |
| **`SIGSTOP` → 杀子孙** | 同上 | **POSIX 信号，Windows 没有** | 用 `psutil`（需加依赖）或 `taskkill /T`；哨兵当前依赖数只有 4 个，**建议不引 psutil**，改用 `taskkill /T` |
| **`AbortSignal.timeout`** | `dsh-router/src/sub2api.ts:54` | Python 没有 AbortSignal | `urllib.request.urlopen(..., timeout=15)`；要可取消就自己管 `threading.Event` |
| **FastAPI `/readyz`** | `harnessrouter/gateway/app.py` | 依赖 FastAPI | 哨兵已有 `src/daemon/server.py` 的 `/api/health` —— **直接在那里加 `watchdog_idle` 字段**即可 |
| **`Promise.all` 无取消** | `dsh-omni-router/src/agent-runtime.mjs:213` | 取消不了已经在跑的批次 | Python 必须显式处理：`threading` 用**取消令牌 + 协作式退出**（哨兵 `priority_queue.py` 的 `CancelToken` 已经是这个形态）；`asyncio` 则必须 `gather(..., return_exceptions=True)` |

> ⚠️ **跨平台的最大一条**：harnessrouter 的**僵尸子进程回收**整套依赖 `/proc` + `SIGSTOP`，
> **在 Windows 上不可用**。抄它的**判定逻辑**可以，抄**执行手段**必须重写。

---

## 6. 落地建议：极简 `TaskRouter`

### 6.1 抄谁

| 抄谁 | 抄哪段 | 落到哨兵哪里 | 优先级 |
|---|---|---|---|
| **`dsh-omni-router`** | `task-decision.mjs` 的**单一决策对象 + 派生级联** | `src/core/session_server.py` 新增 `TaskDecision` | **P0** |
| **`dsh-omni-router`** | `mission-dag.mjs` 的**就绪集 + 写冲突批次** | `src/core/priority_queue.py` 的 `drain()` 编排层 | **P1** |
| **`harnessrouter`** | `control_store.py` 的**带 fencing 的会话租约** | `src/daemon/context.py` / `session_server.py` | **P0**（补会话并发空洞） |
| **`harnessrouter`** | `_OpsDrops` **丢但计数** | `src/core/session_server.py` `EventHub._deliver` | **P0**（改动极小、收益立竿见影） |
| **`harnessrouter`** | `runner/server.py:6473-6536` 的**纯 stdlib SSE 转发** | `src/daemon/server.py` | **P2**（若要推事件流） |
| **`dsh-omni-router`** | `evidence-trust.mjs` 的 **T0–T4 分级 + provider 不得自报 T4** | `AlertStore` | **P1** |
| **`dsh-omni-router`** | `recovery-policy.mjs` 的 **6 分支恢复级联** + `strategy-shift.mjs` | `src/llm/base.py` | **P1** |
| **`deepseek-super-agent`** | `defer` / `unavailable` **两个逃生舱** | `TaskRouter` 的标准返回分支 | **P1** |
| **`dsh-omni-router`** | `capability-solver.mjs` 贪心集合覆盖 + `capability-brain.mjs` EMA | — | **P3**（当前无多 Worker 概念） |
| **`dsh-router`** | — | — | ❌ **不抄**（路由的是计费分组） |

### 6.2 目标形态（建议的最小 TaskRouter）

```python
# 概念形态，非最终代码
@dataclass(frozen=True)
class TaskDecision:
    """一个会话/任务只算一次，所有层复用 —— 对齐 omni-router 的 task-decision.mjs。"""
    verdict: str                 # on_track / dangerous / loop
    severity: str                # red_line / standard / none
    priority: int                # P0 / P1 / P2（由 priority_queue.classify_priority 派生）
    risk_reasons: tuple[str, ...]   # 命中理由 —— 让每次判定可追溯
    evidence_level: str          # T1(模型) / T3(规则确定性) / T4(独立复核)
    uncertainty: float           # 显式建模，不再藏进 confidence

class TaskRouter:
    def decide(self, event, session) -> TaskDecision: ...     # 纯函数，O(L)
    def dispatch(self, decisions) -> list[Task]: ...          # 复用 priority_queue
    def admit(self, session) -> Lease: ...                    # fencing 租约
```

**三条设计红线（直接沿用哨兵既有原则）**：
1. **未知即 `defer`，绝不硬猜**（取自 `deepseek-super-agent`）。
2. **拒绝而非代答**：无匹配能力时明确返回不可用，不降级乱挑（取自 `harnessrouter`）。
3. **默认不动手**：TaskRouter 的默认动作是「只记录」，升级到干预必须由显式策略开启（对齐 `intervention-gate.mjs` 的默认 NOOP 与哨兵的 `intervention_mode: advisory`）。

### 6.3 分阶段路线（建议按此顺序，每步都能独立交付）

| 阶段 | 内容 | 依据 | 验收 |
|---|---|---|---|
| **S1**（半天） | `EventHub` 加**丢计数**并经 `/api/state` 暴露 | §4.4 | 灌满一个订阅者队列，确认计数与 `/api/state` 一致 |
| **S2**（1 天） | 引入 `TaskDecision`：`verdict` + `severity` + `risk_reasons` + `evidence_level`；`AlertStore` 落库 | §4.1 + §4.6 | 同一事件在 `basic.py` 与 `priority_queue.py` 得到**同一个** decision 对象 |
| **S3**（1 天） | 会话 **fencing 租约**：同指纹双执行者时，旧持有者写入自动失效 | §4.3 | 并发写同一 session，后者被拒且**不夺走**前者的 fence |
| **S4**（1—2 天） | `priority_queue.py` **接线到引擎**（当前刻意未接）；用 §4.2 的就绪集+写冲突做批次编排 | §4.2 | 多会话并发时 P0 先出队；同会话互斥任务不并行 |
| **S5**（1 天） | `llm/base.py` 加**恢复级联**（同类失败 ≥2 次换策略） | §4.8 | 同一坏点连续失败 2 次后，第 3 次不再重复同一策略 |
| **S6**（可选） | `daemon/server.py` 增 SSE 推流端点 | §4.4 / §5.2 | 按 `runner/server.py:6473-6536` 的两条硬规则实现 |

> **S1—S3 合计约 2.5 天，且互不依赖**，是性价比最高的一段。**S4 才是「TaskRouter」名义上的主体**，但它建立在 S2 的决策对象之上——**顺序不能反**。

### 6.4 特别提醒

- **`priority_queue.py` 已经是全仓最成熟的一块**，不要在 S4 重写它，只在其上加**批次编排层**。它目前**刻意未接线**（文件顶部注释明写「本版刻意不接线到 SentinelEngine」），这正是 S4 的内容。
- **`dsh-omni-router` 与 `harnessrouter` 都没有优先级队列**——**不要指望从它们抄到优先级实现**；这部分哨兵领先，照抄反而是倒退。
- **`harnessrouter` 明确没有熔断器**（`runner/patches/hermes_mcp_breaker.py` 是给第三方 CLI 打的补丁，不是本仓代码）。哨兵若将来需要熔断，得自己写。

### 6.5 watchdog 落地路线（本轮新增）

> **与 §6.3 的 S 阶段并行，互不依赖** —— watchdog 只吃会话状态，
> 不需要 `TaskRouter` 先落地。可以同时开工。

| 阶段 | 内容 | 依据 | 验收 |
|---|---|---|---|
| **W1**（半天） | `/api/health` 增加 `watchdog_idle` 字段（**只读**，零副作用） | §4.9 ★⑬ | 健康端点能报出「没有卡住的会话」 |
| **W2**（1 天） | 会话级**三态判定**（`stale` / `failed_jobs` / `goal_drift`）+ **三重地板** + 两条豁免（人为暂停、进展≠豁免）。**先只判定、不干预** | §4.9 ★⑨ ★⑩ | 人为制造一个「心跳停更」的会话能被认出来；新建会话不会被误判 |
| **W3**（1 天） | **两阶段升级**：先 nudge（往会话日志追加一条追问），仍不醒才 fail；每个会话**只 nudge 一次** | §4.9 ★⑨ | nudge 后会话恢复 ⇒ 不再升级；不恢复 ⇒ 只升级一次 |
| **W4**（1 天） | 失败**结算与回滚** + **硬上限从「本轮开始」起算**（心跳不许续命）+ 反误杀四防线 | §4.9 ★⑩ ★⑫ | 心跳持续刷新但超过硬上限 ⇒ 仍然判死；结算前重读，已终态则让位 |

> ⚠️ **W2 之前必须先做一件事**：把「会话心跳」写进 `SessionRegistry`。
> 哨兵目前**没有任何会话心跳** —— 现有的只有**引擎**心跳（`engine_watchdog.py`）。
> 没有会话心跳，W2 无从谈起。
>
> ⚠️ **W3 的 nudge 有一条安全红线**：nudge 是**往会话日志里追加一条消息**，
> **不是**向 Agent 发指令、**不是**杀进程、**不是**动文件。
> 哨兵是**只读观察者**（§4.9 之外那条红线，v5.0.0 就定死了）——
> **写会话日志这一步要单独评估**：它算不算「写入被监控方」，
> 需要在设计时明确表态，不能默认。

---

## 7. 风险与免责

1. **本报告为静态阅读结论**，未运行任何被测代码。`dsh-omni-router` 与 `deepseek-super-agent` 有完整单测（`test/` 目录），但**本次未执行**（遵守任务约束：不安装依赖、不运行不明脚本）。文中标注的复杂度均为**按代码结构静态推算**，未经基准测试。
2. **上游实现自带缺陷**，抄的时候必须修：§5.3 末尾列出的两处（`confidence` 语义冲突、`superseded` 状态漏判）。**照抄缺陷比不抄更糟。**
3. **`harnessrouter` 的 `gateway/` 是生产级网关**（含租户隔离、计费、多副本），其并发/背压设计**远比哨兵所需复杂**。§4.3 的租约算法**可以整体摘出**（纯逻辑），但**信号量双闸门、跨副本追尾、Redis pub/sub 不要照搬**——哨兵是单进程无头守护进程。
4. **`临时/` 目录下的源码未被修改**，本次仅执行只读的文件枚举、文本读取与内容检索。
5. **上游许可需在真正复制代码前复核**：本次**未复制任何上游源码**，只提取算法与设计思路。若后续要粘贴具体实现片段，需先核对各仓 `LICENSE`（`dsh-omni-router` MIT、`harnessrouter` 见其仓内声明、`deepseek-super-agent` MIT、`dsh-router` MIT）并履行署名义务。
6. **watchdog 章节（§2.5 / §4.9 / §5.5 / §6.5）的额外免责**：
   - `deepseek-super-agent` 的 watchdog **有完整单测**（`test/watchdog-fail.test.ts`、
     `test/health.test.ts`），但**本次仍未执行** —— §2.5 引用的行为契约是**读测试代码得出的**，
     不是「跑出来的绿灯」。
   - `harnessrouter` 的 watchdog 机制**大量依赖其自身的生产事故复盘**（代码注释里记着
     2026-10-19 / 2026-10-21 两次误杀）。这些注释是**上游作者的自述**，
     本次**未做独立复现**，抄的时候应把它当**设计意图**而不是**已验证结论**。
   - 两家都**没有**退避 jitter（`harnessrouter` 全仓无 `import random`）——
     哨兵自己已有，**别把上游的缺陷一起抄进来**。
   - `harnessrouter` 的僵尸回收依赖 **`/proc` + `SIGSTOP`**（Linux 专有），
     Windows 上**必须重写执行手段**（见 §5.5）。
