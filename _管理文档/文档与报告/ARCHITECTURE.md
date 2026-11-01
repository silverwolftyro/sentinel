# 哨兵 Sentinel —— 系统架构设计

> 版本：v3.2.0 · 最后更新：2026-10-28 · 状态：已落地（四层架构 + 守护者模式 + 实时流式 + 意图漂移 + 异步审计 + 异步解释 + 多路 Agent + 远程告警 + 多路会话中枢 + 启动体验优化 + Token 审计 + 告警重试 + 夜间值守晨报 + 多级模型路由 + 单文件打包 + 注入中枢 + 安全点回滚 + 告警合并推送 + 一键复活 + Web Diff + Webhook 真实自检 + 案发时间线 + 开箱安全体检 + 规则热重载 + 绝不闪退的启动路径 + 演示模式显性化 + CLI 进程雷达 + Web 端路由化 + 挂载会话的守护者武装与意图锚定 + 自动锚定与武装建议 + 全局代码卫生 + 移动端适配 + 路由级设置页 + 精准雷达 + 持久化会话记忆 + 手动挂载引导 + 进程守护与开机自启 + **晨报远程投递 + 自身性能监测 + 终局 Night Crawl 演习**）

---

## 1. 设计目标与红线

哨兵的存在意义是：**在人不盯着的时候，替人盯着 Agent**。它不是 IDE 插件，不是 Agent 的
子进程管理器，而是一个**外部的、只读的审计员**。

### 1.1 核心红线（不可违背）

| # | 红线 | 含义 |
| --- | --- | --- |
| R1 | **只读监听** | 哨兵只以只读方式打开被监控的日志文件，不写入、不修改、不截断、不删除任何被监控 Agent 的文件 |
| R2 | **不干预代码** | 哨兵绝不修改被监控 Agent 的源码、配置、prompt、hook 或运行环境参数 |
| R3 | **不注入进程** | 哨兵不向被监控进程注入代码、不 patch、不 hook 运行时；它站在进程之外观察 |
| R4 | **本地闭环** | 送入模型的文本全部来自本机日志，模型推理在本地 Ollama 完成；不向外部发送被监控内容 |
| R5 | **旁路失败** | 哨兵自身崩溃、误报或漏报，不得影响被监控 Agent 的正常运行（审查失败 ≠ 阻断 Agent） |
| R6 | **不终止进程** | 哨兵**绝不 kill** 被监控进程。守护者模式只做可逆的挂起 / 中断，且用户随时可以否决 |

> **v3.3.0：红线一个字都没改，而且 R7 反而更彻底了**。
> 这一版**删掉**了「开机自启」与「守护模式」，
> 只加了两个**只读/可逆**的判定层和一个**只读**的残留检测：
> * **引擎心跳看门狗** `src/core/engine_watchdog.py`（新叶子模块）——
>   每 30 秒只读 `GET /models`，**不触发推理**、**不启动任何进程**；
>   掉线只**告警**（R5：旁路失败），**绝不**替用户拉起引擎；
> * **时间盒** `src/core/timebox.py`（新叶子模块）——
>   「一个子任务吃掉整轮」判成 `[CRITICAL] 严重跑偏`；
>   守护者武装着的时候才挂起，且**可逆**（R6/R7）；
> * **自启能力整体删除** ——
>   `install_startup.bat` / `install_startup.ps1` / 隐藏启动器 VBS /
>   `--daemon` 参数 / 后台自检线程**全部移除**，
>   哨兵**只在你手动启动时**运行，用完即走；
>   残留检测只**提示不删**（删用户系统里的东西是用户的决定，R7）。
>
> **v3.1.0：红线一个字都没改**。
> 这一版做的是「**记住**」「**教会**」「**看住**」三件事，
> **没有扩大**哨兵的动作边界：
> * **挂载持久化** `src/attachments.py`（新叶子模块）——
>   把「挂了哪些日志」写进**哨兵自己**的 `config/attached_sessions.json`。
>   写的是**哨兵的状态**，不是用户的文件（R1）；
>   **绝不**写进用户被监控的项目目录；
> * **压缩日志的「怎么接入」指引** —— 纯**渲染层**的动作。
>   压缩流依然**接不进来**（那个结论一个字没变），
>   只是把「怎么才行」讲清楚；
> * ⚠️ **守护模式 `--daemon` 与开机自启** ——
>   **v3.3.0 已删除**（这一段保留为历史记录）：
>   当时守护模式只重启**哨兵自己**的看板服务（R6 不受影响），
>   开机自启是用户**亲手双击脚本**才发生的**显式操作**；
> * **武装依旧要用户亲手开** —— 持久化只是**恢复**用户当初亲手开的开关，
>   从不自动武装（R7）。
>
> **v3.0.0：红线一个字都没改**。
> 这一版**不新增任何能力模块**，只做四件事：**让它更省心**、**把技术债清干净**、
> **手机上能用**、**把话说清楚**：
> * **自动锚定** —— 挂载会话的目标由哨兵**自动提取**（只读日志前 200 行）。
>   严格来说这只是「把已有的一份信息自动抄进已有的输入框」，
>   中间**没有任何新增动作**（R1）；
> * **武装建议** —— 置信度 ≥ 0.95 时把开关**高亮**出来。
>   **只建议，绝不自动武装** —— 挂起别人的进程永远要用户自己点（R7）；
> * **代码卫生** —— `ruff check .` 零告警；
>   51 处 `except...pass` 全部补上**可定位**的日志（审查侧可以降级，但不能消失，R5）；
> * **移动端适配** —— 纯 CSS + 路由顺序，**不碰**后端契约。
>
> **v2.4.0：红线一个字都没改**。
> 这一版把**挂载进来的会话**从「只读」升级为「全功能」，
> 但**没有扩大**哨兵的动作边界：
> * **反查 PID** `src/procscan.py`（新叶子模块）——
>   从「一份日志」反查「谁在写它」，**四套策略并列**各带置信度；
>   只有置信度 ≥ 0.75 才敢认，查不到就明写「[未绑定进程]」；
> * **守护者依旧要用户亲手武装** —— 反查只是**把选项摆出来**，
>   开关**默认关着**；武装之前挂载会话**只读、只报告**（R7）；
> * **意图锚定** —— 挂载会话**手动注入**目标后**立刻**启用滑窗机制；
> * ⚠️ **顺手修掉一个真 bug**：运行中补建的意图追踪器**没接上**
>   漂移结论回调 —— 手动设定目标后 DRIFT 告警**永远发不出来**。
>   新增 `SentinelEngine.set_intent_tracker()`，装追踪器必接回调。
>
> **v2.3.0：红线一个字都没改**。
> 这一版做的是「**看得见**」与「**说得清**」——
> 哨兵能**发现并接进**用户已经在跑的 Agent（旁路监听，**只读**），
> 界面拆成主看板 + **独立设置页**（配置写回是**外科手术式**的，注释全留）。
> 挂载进来的会话**拿不到守护者** —— 在没弄清对象之前不动别人的进程（R7）。
>
> **v2.2.2：红线一个字都没改**。
> 这一版只做「把已有的事实**说清楚**」——
> 演示模式挂红色横幅 / 斜条纹 / 徽章、会话名加「[演示]」前缀、
> 终端打印 6 行演示说明 —— 全是**渲染层**的动作，
> **一个字节都没写进**被监控的文件，**没有任何新增能力**。
>
> **v2.2.1：红线一个字都没改**。
> 这一版只改**启动路径**的健壮性：双击进演示模式、致命错误不闪退、
> 体检只打一份 —— 三样都**不扩大**哨兵的动作权限。
>
> **v2.2.0：红线一个字都没改**。
> 这一版新增的三样东西（案发时间线 / 开箱安全体检 / 规则热重载）
> **都不扩大哨兵的动作权限**：
>
> | 新能力 | 会不会扩大权限 | 为什么 |
> | --- | --- | --- |
> | 案发时间线 | **不会** | 纯**读**日志，不写任何东西 |
> | 开箱安全体检 | **不会** | 只发两个**极小**的探测请求（`max_tokens=4` / `num_predict=8`） |
> | 规则热重载 | **不会** | 只是**换规则** —— 换出来的规则依然只做「告警 / 挂起」 |
>
> 特别说明热重载：它换的是「**拦什么**」，不是「**能做什么**」。
> 依然不替 Agent 改代码、不擅自终止进程。
>
> **v2.1.0 的红线修订（R6）**：一键复活需要结束那个**被挂起**的进程
> （挂起意味着它正卡在一条危险命令上 —— 放行它等于放行那条命令）。
> 所以 R6 **如实改写**：
>
> | 红线 | v2.0.0 | **v2.1.0** |
> | --- | --- | --- |
> | 终止被监控进程 | **绝不** | **绝不** —— 唯一例外：用户明确点【回滚并重试】，且只针对**哨兵自己挂起过**的 PID |
>
> 作用域由 `Guardian.terminate_suspended()` 钉死：
> 不是哨兵挂起过的进程**连碰都不碰**；保护 PID（0 / 1 / 哨兵自身 / 父进程）一律拒绝；
> 每次终止都写审计留痕。

> **v2.0.0 的红线修订（重要）**：v2.0.0 让哨兵第一次**能动手**，
> 所以 R1 / R2 必须**如实改写**，而不是嘴上说没变：
>
> | 红线 | v1.2.0 | **v2.0.0** |
> | --- | --- | --- |
> | 读被监控日志 | 只读 | **只读**（不变） |
> | 改被监控 Agent 的**源码 / 配置 / prompt** | 绝不 | **绝不**（不变 —— 哨兵不会替 Agent 改代码） |
> | 往 Agent 的**标准输入**写 | 不可能 | **可以**（用户点按钮触发；只写**用户要求的指令**） |
> | 在工作目录里**新建文件** | 不可能 | **可以**（`sentinel_inbox.json` 留言箱 / `.sentinel/safepoints/` 快照） |
> | **恢复**工作目录里的文件 | 不可能 | **可以**（**只恢复用户明确点【回滚】的**文件） |
> | 终止进程 | 绝不 | **绝不**（不变） |
>
> 换句话说，v2.0.0 打破的是「**只读**」这一条，
> 换来了三条**新的、更高**的约束：
>
> | # | 新红线 | 含义 | 落地 |
> | --- | --- | --- | --- |
> | R7 | **可逆** | 哨兵做的每件「动手」的事都能退回去 | 注入写进留言箱有留痕；回滚前先出**计划**，用户点头才执行 |
> | R8 | **可否决** | 用户随时可以说不 | 回滚**必须**先出计划（恢复 N 个 / 删除 M 个），不点就不动 |
> | R9 | **留痕** | 每一次动手都有记录 | 注入写 `injection` 事件到 hub；回滚写 `rollback` 事件；快照有 `manifest.json` |
>
> ⚠️ **「写成功」≠「对方读了」** —— 写进 stdin 只是把字节送到了管道，
> Agent 会不会照做，哨兵**不知道**，也不假装知道。

> **关于「中断」的定义（v0.5.0 起更严格）**：哨兵可以**发出中断信号**
> （告警、通知用户、写入中断请求文件），
> 但**最终是否终止被监控进程必须由用户或上层编排器决定**。
>
> v0.5.0 引入守护者模式后，「中断」有了三种形态，
> 它们的**权限递增程度**是：
>
> | 形态 | `intervention_mode` | 动作 | 可逆 | 谁来撤销 |
> | --- | --- | --- | --- | --- |
> | 仅告警 | `advisory` | 弹窗 + 日志 | —— | —— |
> | 通知 | `notify` | 弹窗 + 音效 + Webhook | —— | —— |
> | 中断请求 | `request_stop` | 写中断请求文件 | —— | 上层编排器 |
> | **守护者** | `guardian` | **挂起 / SIGINT** 被监控进程 | ✅ 随时可恢复 | **用户**（override） |
>
> 守护者**默认关闭**。它只对**红线级**判定动手
> （`severity == "red_line"`），且每次干预都留痕到 `logs/`。
>
> ⚠️ **Windows 上不能用 `os.kill(pid, SIGINT)`** —— CPython 会把它翻译成
> `TerminateProcess`，那是**终止**不是中断。Windows 上的守护者走挂起
> （`psutil.Process.suspend()`）。详见
> [`versions/v0.5.0-guardian.md`](versions/v0.5.0-guardian.md) §4.2。

### 1.2 设计取舍

- **宁可漏报，不可误杀**：规则与模型的阈值偏向高召回告警，处置动作偏向保守。
- **确定性优先**：能用正则和阈值判定的，绝不交给模型（更快、更便宜、更可解释）。
- **可解释**：每条告警都要能回溯到「哪条规则 / 模型的哪段推理」。

---

## 2. 四层架构总览

```
                    被监控 Agent（Claude Code / Codex / Aider / ...）
                              │  stdout / stderr / session JSONL
                              │  （哨兵仅以只读方式打开这些文件）
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ① 采集层  Collection          src/adapters · src/core/tailer         │
│    · adapter 识别 CLI 工具，解析各家私有字段 → 统一事件                │
│    · watchdog 事件监听 + 轮询兜底 + 字节级断点续传                    │
│    · 只读打开文件；不写入、不截断源文件                              │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  SentinelEvent（统一事件契约）
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ② 规则层  Rules（L1）          src/rules                              │
│    · 确定性检测：正则 / 关键字 / 阈值 / 频次                          │
│    · 毫秒级、零外部依赖、结果可复现                                  │
│    · 例：密钥泄漏、越权命令、危险路径写入、提示注入特征                │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  命中事件（可配置是否升级）
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ③ 本地模型层  LLM（L2）        src/llm                                │
│    · 本地 Ollama 推理，语义级判定：真实意图、风险动作、上下文合理性    │
│    · 输入前置脱敏与截断；输出结构化裁决（risk / reason / evidence）   │
│    · 模型不可用时降级为「仅 L1 结论」，不阻塞主流程                    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  裁决结果
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ④ 告警干预层  Alert & Intervention                                   │
│    · 分级：info / warn / critical                                     │
│    · 通道：终端、文件、**桌面弹窗**、**系统音效**、Webhook（钉钉 / 飞书）│
│    · 看板：本地 Web 面板（FastAPI + SSE），只绑回环 + Origin 白名单    │
│    · 干预：默认 advisory（仅告警）；可选 notify / request_stop        │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.1 分层原则

1. **单向依赖**：`core → adapters/rules/llm → alert`，下层不得反向 import 上层。
2. **事件驱动**：各层之间只通过 `SentinelEvent` / `SentinelVerdict` 数据结构通信，不共享可变全局状态。
3. **可插拔**：adapter、规则、模型、告警通道均为可注册组件，新增一种不改动既有代码。

---

## 3. 模块职责边界

| 层 | 目录 | 负责 | **不负责** |
| --- | --- | --- | --- |
| ① 采集 | `src/adapters/` | 识别 CLI 工具；解析其 JSONL / stdout 格式；归一化为统一事件；维护文件读取偏移 | 不做内容判断；不决定告警等级；不调用模型 |
| ① 采集 | `src/core/` | 事件循环；组件装配与生命周期（启动/优雅停止）；配置文件加载；各层调度与背压 | 不实现具体检测规则；不实现具体告警通道 |
| ② 规则 | `src/rules/` | L1 确定性检测；规则注册与匹配；输出命中详情与证据片段 | 不做语义推理；不直接触发告警（只上报结论） |
| ② 规则 | `src/core/engine.py`（`ContextTracker`） | **跨事件**模式识别：连续测试失败 `STUCK`、敏感文件读取后外发 `LEAK`；滑动窗口保留最近 N 个工具事件 | 不做单步判定（那是 `src/rules/` 的事）；不调用模型 |
| ③ 模型 | `src/llm/` | **统一提供方契约**（`LLMProvider`）：prompt 构造；输入脱敏与截断；解析模型输出为结构化裁决；超时与降级；本地 Ollama **或** OpenAI 兼容 API | 不做规则匹配；不决定最终处置动作 |
| ④ 告警 | `src/core/channels.py` + `notifier.py` | 等级判定与过滤；多通道分发（终端/文件/桌面弹窗/系统音效/Webhook）；失败逐个隔离 | 不修改被监控 Agent；不终止进程（默认模式） |
| ④ 告警 | `src/gui/server.py` | 本地看板：增量跟随两个日志文件；SSE 实时推送告警卡片；触发扫描；**运行中切换任务目标**（`TaskGoal`）；**下发纠偏指令 / 一键回滚**（v2.0.0） | 不写告警日志（那是引擎的地盘）；不绑非回环地址；**不 import 引擎**（走回调注入） |
| ④ 告警 | `src/core/injector.py`（v2.0.0） | 把纠偏提示**送到 Agent 手里**：stdin 直写 → 留言箱降级 → 重启注入 | 不替 Agent 决定怎么做；不假装送达（写成功 ≠ 对方读了） |
| ④ 告警 | `src/core/safepoint.py`（v2.0.0） | 工作目录的**极速快照**与**一键回滚**；先出计划再动手 | 不碰工作目录**之外**的文件；不回滚排除目录（`.git` / `node_modules` …） |

> **v0.7.0 补充**：实时流式监听不改变分层结构 ——
> 采集层多了一个 `tail_stream()`（同一套「统一事件」契约），
> 核心层多了一个 `IntentTracker`（**跨事件**模式识别，
> 与 `ContextTracker` 同级，同样「只上报结论、不决定处置」）。
> 漂移告警的 `severity` 是 `standard`，
> 因此**守护者不会碰它** —— 漂移只警告，不干预。

> **v0.8.0 补充**：
> * **异步漂移审计** —— `IntentTracker` 的判定搬进后台线程 + 有界队列，
>   主事件循环**绝不被模型调用堵住**；队列满了按背压丢弃并留痕。
>   这是「审查侧绝不影响被监控方」（红线 R5）在模型层的落地。
> * **引擎单步入口** —— `SentinelEngine.process_event()` 让外部脚本
>   （真实 Agent 管道接管）可以直接喂事件，不必先把日志落盘。
> * **自动锚定** —— 引擎从流里第一条 `user_message` 事件提取任务目标，
>   优先级：显式（命令行 / 看板）> 自动 > 配置文件。

> **v0.9.0 补充**：
> * **异步解释** —— L2 的「人话解释」也从主循环搬走：
>   `ExplanationWorker`（后台线程 + 有界队列 `max_pending=4`）。
>   引擎的告警出口统一为 `_publish_alert()`，顺序**不可调换**：
>   **① 立刻干预 → ② 标记 `explanation_pending` → ③ 发出原告警 → ④ 排队解释**。
>   原文警必须先落盘，否则解释线程抢先返回时更新事件会「找不到卡片」。
> * **告警对号** —— 每条告警带 `alert_id`（`{毫秒时间戳}-{6位序号}`）。
>   解释补充靠它与原告警配对，贯穿「引擎 → 日志 → 看板 store → SSE → 前端 DOM」。
>   这是「追加更新」而非「追加一张卡片」的全部机制基础。
> * **多路 Agent** —— 采集层新增 `codex.py` / `claude.py` 适配器；
>   与产品无关的**消息容器解析**上提到 `BaseAdapter`，
>   各产品适配器只写自己特有的一部分。
> * **守护者的目标 PID** —— 区分两种启动形态：
>   **启动器自己就是 Agent**（原生二进制，如 `codex.exe`）→ 直接用启动器 PID；
>   **启动器是壳**（npm `.cmd` 垫片）→ 往下找并**跳过所有壳进程**。
>   另有 `pid_resolver` 兜底：拦截时若报 `NoSuchProcess`，重新解析一次再试。
> * **看板的「当前监控目标」** —— `WatchTarget` 快照 + `GET /api/watch_target`；
>   展示 Agent 类型、任务目标、以及目标是**自动提取**还是手动指定。
>
> **v1.0.0 补充**：
> * **远程告警通道** —— `WebhookChannel` 正式实现并启用，支持
>   `dingtalk | feishu | serverchan | bark | generic` 五种报文格式。
>   钉钉与飞书支持**加签**（两家算法不同，各一个独立函数）。
>   卡片正文由 `format_remote_card()` 渲染，**必含五要素**：
>   Agent 名称 / 触发规则 / 触发类型 / 任务目标 / 当前操作。
> * **远程触发的触发标记** —— 告警带 `triggers` 字段，取值
>   `guardian_intercept`（守护者**真的拦下来**才打）/ `intent_drift` / `context_anomaly`。
>   远程通道默认**只推前两类** —— 半夜一条 STUCK 就能把人吵醒，那等于没开。
> * **防骚扰** —— 按**事件指纹**（`sha1(agent|rule_id|command)`）在
>   `dedup_seconds`（默认 60 秒）窗口内去重；**推送失败撤销占位**允许重试。
> * **多路会话中枢** —— `SessionSpec` / `Session` / `SessionRegistry`；
>   看板可**同时监听多个**被监控流，SSE 事件带 `session_id`。
>   `registry.get()` 保留「找不到回落主会话」的兼容行为，
>   会话级 API 一律用**严格**的 `registry.resolve()`（找不到 → 404）——
>   悄悄给错数据比报错更糟。
> * **告警上下文标签** —— 引擎产出的每条告警都带 `agent` / `task_goal` / `triggers`，
>   「谁闯祸 / 它本来该干什么 / 为什么被拦」在**产生的那一刻**就写清楚。
> * **引擎的 `on_explanation` 公开钩子** —— `ExplanationWorker` 在**构造时**
>   就抓住了绑定方法，事后再替换引擎属性**完全无效**；
>   所以引擎提供公开钩子，观察者异常一律降级。
>
> **v1.0.1 补充**（只加「给人看的部分」，审查逻辑一行未动）：
> * **终端呈现层** `src/console.py` —— **叶子模块**（只用标准库，
>   不依赖哨兵任何内部模块），所以应用层 import 它时
>   不会把 `src.core.engine` 拖进来，解耦原则不受影响。
>   负责：ASCII 大字报、看板就绪提示、端口冲突提示、
>   延迟自动开浏览器、端口探测。
> * **就绪提示要「真的连上了」才说** —— `announce_ready_when_up()`
>   在后台线程轮询端口，**连得上**才打印「已就绪」并开浏览器。
>   启动前就宣称就绪，点进去会撞「连接被拒绝」。
> * **端口占用给「人话」** —— 启动前 `port_is_free()` 真去 bind 一下
>   （**刻意不设** `SO_REUSEADDR`，否则在 Windows 上测不出占用），
>   占用则输出「端口被占用，请修改端口或关闭已有进程。」，
>   **不抛红色异常栈**。
> * **演示模式** `src/demo.py` —— 生成**模拟**的 mcode `stream-json` 日志，
>   格式与真实流完全一致，走同一套适配器与规则引擎；
>   演示里刻意放一条**红线级**命令，看板上会亮起真实的红线卡片。
>   日志里的命令**只是被审查**，绝不会被执行。
> * **CLI 进程雷达** `src/radar.py`（v2.3.0）—— **叶子模块**（只用标准库）。
>   哨兵的新定位是**雷达**：用户**已经在终端里**跑着 Agent 了，
>   哨兵不该要求「必须从我这儿启动」。
>   它扫 `~/.codex` / `~/.mcode` / `~/.claude`（可用
>   `SENTINEL_RADAR_ROOTS` 追加目录），找出**正在被写入**的日志，
>   认出是 Codex / MCode / Claude，并从日志里挖出工作区。
>   ⚠️ **只读**：它一个字节都不会改被监控的日志。
> * **进程反查** `src/procscan.py`（v2.4.0）—— **叶子模块**（只用标准库）。
>   挂载进来的会话要能被守护者接管，哨兵得先知道**该挂谁**。
>   它从「一份日志」反查「正在写它的进程」，
>   **四套策略并列**各带置信度：
>   `cmdline_path`（0.95，命令行里带着这个路径）、
>   `open_files`（0.95，进程打开着这个文件）、
>   `cmdline_cwd`（0.85，Agent 特征 + 工作目录）、
>   `cwd_only`（0.60，**不够**，不允许武装）。
>   ⚠️ `psutil` 是**可选依赖**（函数内惰性导入），缺了整体降级成「查不到」。
>   ⚠️ 打包版（PyInstaller 单文件）会再起一个同名子进程 ——
>   `protected_pids()` **专门排除**所有与哨兵同名的进程，
>   绝不把自己当成被监控对象。
> * **`--no-browser`** —— 看板服务与启动器都支持，
>   适合远程 / 无桌面环境。

> **v3.1.0 补充**（记住 · 教会 · 看住）：
> * **挂载持久化** `src/attachments.py` —— **叶子模块**（只用标准库）。
>   把「挂了哪些日志」写进**哨兵自己的** `config/attached_sessions.json`，
>   哨兵重启后自动接回来。
>   ⚠️ 写的是**哨兵的状态**，**绝不**写进用户被监控的项目目录（R1）；
>   整个模块**不抛异常** —— 存不下来只是存不下来，
>   挂载本身照样成功（R5）。
> * **压缩日志的「怎么接入」指引** —— 纯**渲染层**的动作：
>   按钮从「禁用」改成「能点、点了给指引」
>   （禁用按钮**收不到 click**，用户点了没反应），
>   外加设置页的「如何让我的 Agent 被监控」折叠面板。
>   ⚠️ 压缩流依然**接不进来** —— 那个结论一个字没变。
> * ⚠️ **守护模式 `--daemon` 与开机自启 —— v3.3.0 已删除**
>   （这一段保留为历史记录）：
>   当时守护模式是后台自检线程用 HTTP 探测 `/api/sessions`，
>   连续 3 次探不到就判定服务已死，记日志并**自动重启** uvicorn
>   （重启的是**哨兵自己**的看板服务，不是被监控的 Agent，R6）；
>   开机自启是 `install_startup.bat` + `install_startup.ps1`
>   在 Windows「启动」文件夹放一个 `wscript.exe` 快捷方式，
>   由它用 VBS 以「隐藏窗口」的方式拉起哨兵。
>   ⚠️ **v3.3.0 起这些能力全部删除** ——
>   看板崩了就退出（不自己复活），哨兵**只在你手动启动时**运行。
>
> **v3.3.2 补充**（雷达也看进程 · 快捷重定向命令）：
> * **活跃 Agent 进程枚举** `src/procscan.py`（仍是**叶子模块**）——
>   雷达扫的是**文件**；这一条扫的是**进程**。
>   两者互补：文件找不到的时候，进程往往还在 ——
>   那正是用户「终端里跑着任务、看板却抓不到」的那一刻。
>   * 认一个进程是不是 Agent，**两层信号缺一不可**：
>     进程名在 `AGENT_HOST_IMAGES` 里（`node.exe` / `mcode.exe` …）
>     **且**命令行命中该 Agent 的**特征**
>     （`@minimax-ai/code` / `@openai/codex` / `@anthropic-ai/claude-code`）。
>     只看进程名会把满机的 Node 进程算进来；
>     只看宽泛片段会把 `npx rea-agents` 这类非 Agent CLI 误判 ——
>     提示一旦变成「狼来了」，用户就再也不信了；
>   * 同一个 Agent 在**同一工作目录**下的多个进程合并成一个：
>     用户关心的是「有几个任务在跑」，不是「有几个 node 进程」；
>   * 判「有没有被监控」用**三条信号**，从**确定**到**推断**：
>     命令行带着正在监控的日志路径（确定）→
>     会话工作目录就是进程工作目录（确定）→
>     同类型 Agent 的日志**落在雷达扫描目录内**且**十分钟内被写过**（推断）。
>     ⚠️ 第 ③ 条是推断，条件卡得很死 ——
>     **宁可多提示一句，也不能让用户以为「被监控了」其实没有**；
>   * `GET /api/process_activity` —— 带 **8 秒缓存**
>     （枚举进程不便宜，而看板每 10 秒问一次）；
>   * ⚠️ **只读**：它只**看**进程表（进程名 / 命令行 / 工作目录），
>     不给任何进程发信号、不改任何进程状态（R1 / R3 / R6）；
>   * ⚠️ `psutil` 仍是**可选依赖** —— 缺失时整体降级，
>     接口照常返回（`available: false`），界面不显示提示（R5）。
> * **快捷重定向命令**（挂载管理页）—— **纯渲染层**的动作：
>   服务端把「当前机器的工作目录 + 落盘路径」渲染进页面，
>   前端按所选 Agent 拼出命令。
>   ⚠️ 它**只生成字符串**，不代跑任何命令；
>   落盘目录由服务端**提前建好**（否则 shell 的 `>` 会直接报错）。
>
> **v2.0.0 补充**（控制中枢 —— 哨兵第一次**能动手**）：
> * **注入中枢** `src/core/injector.py` —— 住在**告警干预层**（第 ④ 层）。
>   看板与启动器是**两个进程**，看板**没有** Agent 的管道句柄 ——
>   所以注入走**留言箱文件做跨进程 IPC**，启动器做中继：
>   能写 stdin 就写，不能就留在箱里等下次启动注入任务提示。
>   **降级路径与成功路径是同一条链路**，没有两套代码。
> * **安全点** `src/core/safepoint.py` —— 同样住在**告警干预层**。
>   快照在 `guardian.intervene()` **之前**拍 —— 拦截完再拍就晚了。
>   回滚**目标**是「拦截发生时**生效**的安全点」（默认会话起点的基线），
>   而不是拦截瞬间那张（那张已经包含了损坏，回滚它是空操作）。
> * **★ 看板依然不认识引擎** —— 新功能一律走**回调注入**：
>   `briefing_callback` / `inject_callback` / `rollback_callback`。
>   没有回调时看板**惰性导入** `src.core.injector` 降级处理，
>   引擎相关的 import **一个都没有进看板的顶层**。
> * **通知分「记录型」与「通知型」** —— console / file 是流水账，一条不少；
>   desktop / sound / webhook 是敲门，走合并窗口。
>   **红线级永远直通** —— 合并绝不能盖住要紧的事。


**边界红线（容易越界的地方）**：

- `src/rules` **不允许** 直接 `import requests` 或调用模型 —— 规则层必须保持纯净与快速。
- `src/llm` **不允许** 读取配置文件路径以外的本地敏感文件，仅接收传入的事件片段。
- `src/adapters` **不允许** 写入被监控目录，包括「顺手帮忙清理」这类操作。
- 任何层都**不允许**在异常时让 Agent 侧失败 —— 审查侧异常一律降级并记录。

---

## 4. 核心数据契约

### 4.1 统一事件（采集层 → 规则层）

> **v0.2.0 已落地**：事件是**普通 `dict`**（保持 JSON 友好，可直接落盘与跨进程传递），
> 不是 dataclass。契约如下：

```python
{
  "source":     "mcode",        # 归一化来源标识
  "type":       "tool_call",    # thinking / tool_call / text / result / unknown
  "session_id": None,           # 会话 ID，可能缺失
  "timestamp":  1750000000.0,   # 事件自带时间戳；没有就是 None（绝不伪造时间）
  "tool": {                     # 仅 tool_call 事件有值
      "name":      "Bash",
      "arguments": {"command": "rm -rf /"},
      "command":   "rm -rf /",  # 从参数中尽力提取的命令文本
  },
  "text":       "",             # thinking / text 事件的正文
  "raw":        {},             # 原始 JSON 载荷，便于回溯
}
```

字段缺失时的取值约定：

- `timestamp`：载荷里没有时间戳就是 `None`，**不用当前时间顶替**（避免伪造事实）；
- `session_id`：没有就是 `None`；
- `tool`：非 `tool_call` 事件一律为 `None`；
- `text`：非文本类事件为空串。

### 4.2 规则判定（规则层 → 告警层）

> **v0.2.0 已落地**：`RuleEngine.check_event()` 返回普通 `dict`，
> **必定包含** `verdict` 与 `reason` 两个键。
>
> **v0.5.0 追加**：`severity`（严重度）与 `category`（红线类别）两个键。
> **只增不改** —— 既有键的语义完全不变，老代码不受影响。

```python
{
  "verdict":    "DANGEROUS",    # ON_TRACK / DANGEROUS / LOOP
  "reason":     "匹配到高危命令: rm -rf",
  "rule_id":    "dangerous_command",  # 或 "repeat_read_loop" / None
  "evidence":   "rm -rf",              # 触发判定的片段
  "level":      "critical",            # info / warn / critical
  "confidence": 1.0,
  "pattern":    r"\brm\s+(?:...)",      # 命中的正则原文
  "severity":   "standard",            # red_line / standard / none
  "category":   None,                  # root_delete / mass_delete / ...
}
```

#### 严重度：为什么要有 `red_line`

`DANGEROUS` 是一档，但 ``chmod 777`` 与 ``rm -rf /`` 的后果差着数量级。
守护者要动手，就必须先知道「哪一条值得动手」。

| 严重度 | 含义 | 守护者 |
| --- | --- | --- |
| `red_line` | 一旦执行就回不来 | **可以干预** |
| `standard` | 危险，但通常可恢复 | 只告警 |
| `none` | 无异常 | 无 |

红线类别与判定方法见
[`versions/v0.5.0-guardian.md`](versions/v0.5.0-guardian.md) §3。

### 4.3 事件流

```
SentinelEvent ──▶ L1 规则引擎 ──▶ [命中] ──▶ L2 模型审查 ──▶ 告警层
                                          └─[未命中] ──────────────────┘
```

未命中 L1 的事件默认**不进入** L2（避免成本与延迟爆炸）；是否对全部事件做 L2 审查由配置控制。

> **v0.2.0 现状**：L2 尚未接入，事件流在 L1 之后直接进入告警层。

### 4.4 L2 解释结果（模型层 → 告警层）

> **v0.3.0 已落地**：`OllamaClient.explain()` 返回结构化结果。

```python
{
  "text":       "清理临时构建产物。原因：…；影响：…；后果：…",
  "degraded":   False,      # 是否走了降级路径
  "reason":     "",         # 降级原因；未降级时为空串
  "detail":     {"title": ..., "why": ..., "touch": ..., "outcome": ...},
  "latency_ms": 1840,
  "model":      "qwen2.5-coder:14b",
}
```

**L2 的三条硬约束**：

| # | 约束 | 落地位置 |
| --- | --- | --- |
| 1 | **永不抛异常** —— 任何故障都降级为「原始命令 + 规则原因」 | `base.LLMProvider` 全部失败路径 |
| 2 | **只允许本地端点** —— 非回环 `base_url` 拒绝调用 | `ollama_client._is_loopback` |
| 3 | **不参与生死判定** —— 只说人话，不建议放行或终止 | `prompts.SENTINEL_EXPLAIN_TEMPLATE` |

> **v3.2.1 补充四条本地引擎的约束**（本机 NInfer 实机踩出来的）：
>
> | # | 约束 | 落地位置 |
> | --- | --- | --- |
> | 4 | **同一台引擎串行** —— 单并发引擎并发两个请求会**直接崩** | `base.engine_lock_for` + `api_client.chat` |
> | 5 | **本机超时不低于 600 秒** —— 27B 推理模型光「思考」就几十秒 | `api_client.LOCAL_ENGINE_MIN_TIMEOUT` |
> | 6 | **思考过程不得进告警文案** —— 只取最终答案 | `parsing.strip_thinking` + `api_client._extract_content` |
> | 7 | **本机地址不算「数据出本机」** —— 那句话对着 `127.0.0.1` 是假话 | `api_client.is_local_endpoint` |

> **降级 ≠ 放行**。模型缺失时，告警**照常产生**，规则判定**照常保留**，
> 只是少一段人话解释。这条契约抄自旧项目
> 「永不抛异常，缺失绝不等同于安全」。

### 4.5 前后端各持一份的常量（v3.0.0 补齐）

日志格式与阈值这类**跨语言契约**，前后端**各持一份**常量 ——
不为这么点值引入一次网络往返。

| 常量 | 后端 | 前端 | 值 |
| --- | --- | --- | --- |
| `ALERT_JSON_MARKER` | `src/core/engine.py` | `src/gui/static/dashboard.js` | `"ALERT_JSON"` |
| `ALERT_UPDATE_MARKER` | `src/core/engine.py` | `src/gui/static/dashboard.js` | `"ALERT_UPDATE_JSON"` |
| `TOKEN_MARKER` | `src/core/engine.py` | `src/gui/static/dashboard.js` | `"TOKEN_JSON"` |
| `EVENT_MARKER` | `src/core/engine.py` | `src/gui/static/dashboard.js` | `"EVENT_JSON"` |
| `ONTRACK_MARKER` | `src/core/engine.py` | `src/gui/static/dashboard.js` | `"ONTRACK_JSON"` |
| `ARM_SUGGEST_CONFIDENCE` | `src/gui/server.py` | `src/gui/static/dashboard.js` | `0.95` |

> ⚠️ **两份必须相等** —— 有回归测试钉住（`tests/test_v300_release.py`）。
> 改了一边忘了另一边，症状是「高亮了但阈值不对」这类很难查的问题。

> **`ONTRACK_JSON`（v3.3.1 新增）** ——
> 引擎把 L1 判定为 `ON_TRACK` 的事件压成**一行极简摘要**
> （`正常读取: auth.py`）写进告警日志，
> 看板据此渲染面板底部的「实时审核流水」。
> 它**不是**告警 —— 单独解析，不混进告警列表。
> 摘要由后端 `src/core/live_audit.py` 生成（前端只渲染），
> 这样「极其精简」才有唯一的解释者。

### 4.6 前端的两个硬约定（v3.0.0）

| 约定 | 位置 | 为什么 |
| --- | --- | --- |
| **先 `teardown()` 再 `fetch()`** | `src/gui/static/router.js` 的 `navigate()` | Agent 跑得飞起时 SSE 回调全在**主线程**上做 DOM 操作。`fetch` 的 `.then()` 排在事件循环里只能等它们跑完 —— 于是「点设置 → 页面卡半秒」。先关 EventSource，主线程立刻空出来。 |
| **窄屏媒体查询放文件末尾** | `src/gui/static/dashboard.css` | CSS **同优先级后写的赢**。窄屏块写在中间的话，后面 v2.2.0 时间线 / 体检 / 演示横幅的样式会覆盖它，移动端适配等于白写。 |



---

## 5. 配置

主配置位于 `config/config.yaml`（本地文件，不入库）。关键开关：

| 配置项 | 默认 | 作用 |
| --- | --- | --- |
| `collection.watch_dirs` | `[]` | 需要监听的日志目录 |
| `rules.enabled` | `true` | 是否启用 L1 |
| `rules.escalate_to_llm` | `true` | L1 命中后是否送 L2 复核 |
| `llm.enabled` | `true` | 是否启用 L2（关闭则纯规则模式） |
| `llm.redact_before_llm` | `true` | 送模型前脱敏（同时用于告警落盘） |
| `llm.keep_alive` | `0` | 模型驻留时长；`0` = 用完即卸载（省显存） |
| `llm.failure_threshold` | `3` | 连续失败几次后触发熔断 |
| `llm.breaker_open_seconds` | `300` | 熔断持续时长（秒） |
| `llm.routing.enabled` | `true` | 是否启用多级模型路由（v1.2.0） |
| `llm.routing.escalate_on_red_line` | `true` | 红线级拦截是否升级到强模型 |
| `llm.routing.drift_escalate_threshold` | `1` | 连续判漂移几次后升级 |
| `llm.routing.fallback.*` | — | 升级用的模型（`provider` / `base_url` / `model` / `api_key_env` / `max_tokens` …） |
| `alert.min_level` | `"info"` | 告警等级下限 |
| `alert.intervention_mode` | `"advisory"` | 干预强度，默认仅告警 |
| `alert.channels[].type` | — | `console` / `file` / `desktop` / `sound` / `webhook` |
| `alert.channels[].format` | `"generic"` | Webhook 报文格式：`dingtalk` / `feishu` / `serverchan` / `bark` / `generic` |
| `alert.channels[].card` | `false` | 发送 Markdown 卡片而不是纯文本 |
| `alert.channels[].secret` | `""` | 钉钉 / 飞书的加签密钥 |
| `alert.channels[].triggers` | `null` | 只推带这些触发标记的告警；**留空 = 推全部** |
| `alert.channels[].dedup_seconds` | `60` | 防骚扰窗口（秒）；`0` = 不限制 |
| `alert.merge_seconds` | `60` | **通知型通道**的告警合并窗口（秒）；`0` = 不合并。⚠️ **红线级永远直通** |
| `alert.webhook_startup_test` | `true` | 启动时**真的**往真实 Webhook 发一条自检消息（v2.1.0）。⚠️ 不是模拟，手机会响 |
| `rules.hot_reload.enabled` | `true` | 是否启用规则热重载（v2.2.0）—— 改 `config.yaml` 不用重启 |
| `rules.hot_reload.interval` | `1.0` | 盯盘间隔（秒）—— 文件改动后大约这么久生效。⚠️ 用的是 **mtime 轮询**，不是 watchdog |
| `safe_point.enabled` | `true` | 是否启用安全点快照（v2.0.0）；关掉就完全不往用户目录里写东西 |
| `safe_point.root` | `""` | 快照存放目录；留空 = `<工作目录>/.sentinel/safepoints` |
| `safe_point.max_bytes` | `67108864` | 目录总量超过这个值就降级成增量模式（默认 64 MB） |
| `safe_point.max_files` | `20000` | 文件数超过这个值就降级成增量模式 |
| `safe_point.max_seconds` | `3` | **时间预算**（秒）—— 超时立刻收手，保证「极速」 |
| `safe_point.recent_minutes` | `60` | 增量模式只备份最近改动过的文件（按 mtime 筛） |
| `intervention.enabled` | `true` | 是否启用看板【直接发送给 Agent】（v2.0.0） |
| `intervention.inbox_name` | `"sentinel_inbox.json"` | 留言箱文件名（落在被监控 Agent 的工作目录里） |
| `intervention.max_inbox_messages` | `20` | 留言箱最多留几条；超出丢弃最旧的 |
| `alert.channels[].max_retries` | `2` | Webhook 额外重试次数（v1.1.0）；指数退避 `retry_backoff × 2ⁿ` |
| `alert.channels[].retry_backoff` | `1.0` | Webhook 首次退避秒数（v1.1.0） |
| `context.window` | `5` | 上下文追踪器保留最近几个工具事件 |
| `context.stuck_threshold` | `3` | 连续几次测试失败判定为「卡住」 |
| `task_goal` | `""` | 本次任务的**原始目标**（意图锚点）；留空 = 不做漂移判定 |
| `drift.enabled` | `true` | 是否启用意图漂移检测 |
| `drift.window` | `5` | 每收集几个「思考 + 工具调用」事件做一次判定 |
| `drift.threshold` | `2` | 连续几次判定为漂移才告警（5 × 2 = 10 步） |


---

## 6. 扩展指南

### 6.1 新增一个 CLI 适配器

1. 在 `src/adapters/` 新建 `<cli_name>.py`，继承 `BaseAdapter`；
2. 实现 `matches(line) -> bool`（识别该工具的日志行）与 `parse(line) -> SentinelEvent | None`；
3. 在 `src/adapters/__init__.py` 的 `REGISTRY` 中注册；
4. 在 `tests/` 补充该工具真实日志样本的解析用例。

### 6.2 新增一条 L1 规则

1. 在规则包中实现 `match(event: SentinelEvent) -> SentinelVerdict | None`；
2. 声明 `rule_id`、`level`、`description`；
3. 提供至少 2 条正例与 2 条负例测试；
4. 规则必须是**确定性**的：相同输入必须得到相同输出。

### 6.3 新增一个告警通道

实现统一的 `send(verdict) -> None` 接口并注册到通道列表；通道失败必须**被隔离**，
不得影响其他通道与主流程。

---

## 7. 目录与模块映射

> **v0.7.0 现状**（✅ = 已落地，⬜ = 待后续版本）：

```
src/
├── config_loader.py       ✅ 配置加载（叶子模块，不依赖任何内部模块）
├── tailer.py              ✅ FileTailer：文件尾随（轮转 / 截断 / 半行），叶子模块
│                          ✅ 采集层与看板共用同一份实现，避免分叉
│                          ✅ v3.2.0：max_buffer 硬上限（1 MiB）—— 超长行不撑爆内存
├── selfmon.py             ✅ 哨兵自身的资源监测（v3.2.0）：叶子模块，只用标准库
│                          ✅ ProcessMonitor：CPU / 内存 / 运行时长 / 线程数
│                          ✅ Windows 走 ctypes 调 Win32 API（**不引新依赖**）
│                          ✅ 取不到的项一律 None —— **绝不编造数字**
├── console.py             ✅ 终端呈现层（叶子模块，只用标准库）
│                          ✅ build_banner：ASCII 大字报（块状字模，按词换行）
│                          ✅ print_ready / print_port_busy：人话提示
│                          ✅ port_is_free：真去 bind 一下，测端口占用
│                          ✅ open_browser_delayed：延迟自动开浏览器
│                          ✅ v2.2.1：report_fatal / wait_for_user / fatal_guard
│                          ✅ v2.2.1：「绝不闪退」—— 出错先说人话，再挂住等用户看完
│                          ✅ v2.2.1：只在**真终端**挂起（stdin.isatty）
│                          ✅ announce_ready_when_up：等端口连上了才提示就绪
│                          ✅ v2.2.2：print_demo_notice —— 演示模式 6 行醒目说明
│                          ✅ v2.2.2：编码兜底（GBK 重定向不炸启动）
├── demo.py                ✅ 演示模式：生成模拟的 Agent 流式日志（mcode 格式）
├── core/
│   ├── engine.py          ✅ SentinelEngine：串联四层 + 告警输出 + L2 接入
│   │                      ✅ ContextTracker：STUCK / LEAK 跨事件模式
│   │                      ✅ IntentTracker：5 步一窗摘要 + 连续 2 次判定漂移
│   │                      ✅ 异步判定：后台线程 + 有界队列 + 背压丢弃
│   │                      ✅ process_event()：单步入口（外部脚本可直接喂）
│   │                      ✅ 自动锚定：从首条 user_message 提取任务目标
│   │                      ✅ ExplanationWorker：L2 解释异步化（有界队列 + 背压）
│   │                      ✅ _publish_alert()：干预 → 标记 → 原告警 → 排队
│   │                      ✅ alert_id：毫秒时间戳 + 6 位序号（追加更新靠它配对）
│   │                      ✅ agent / task_goal / triggers：告警上下文标签
│   │                      ✅ on_explanation：公开钩子（观察者异常只降级）
│   │                      ✅ 守护者接入：仅 severity=red_line 才干预
│   │                      ✅ v1.1.0：专属 logger（sentinel.alert.<uuid8>）防多路串台
│   │                      ✅ v1.1.0：Token 台账接线 + TOKEN_JSON 账单行（节流 100）
│   │                      ✅ v2.0.0：safe_point_service / workdir 接线
│   │                      ✅ v2.0.0：_ensure_baseline_safe_point()（不重复打基线）
│   │                      ✅ v2.0.0：_capture_safe_point() 在挂起**之前**拍快照
│   │                      ✅ v3.2.0：selfcheck() 周期自检（CPU / 内存 / 运行时长）
│   │                      ✅ v3.2.0：L2 判定降级时终端**明确提示**（静悄悄的失效最坏）
│   ├── guardian.py        ✅ Guardian / ProcessController：可逆干预 + 用户放行
│   │                      ✅ pid_resolver：目标 PID 失效后重新解析一次再试
│   ├── tokens.py          ✅ Token 台账（v1.1.0）：估算 + usage 真值 + 「挽回」公式
│   │                      ✅ estimate_tokens：CJK 1 字 ≈ 1 Token、其它 4 字符 ≈ 1 Token
│   │                      ✅ extract_usage：codex / OpenAI 兼容 / usage 平铺三形态
│   │                      ✅ usage 是**快照**不是增量：真值来了替换估算值
│   ├── briefing.py       ✅ 夜间值守晨报（v1.2.0）：统计 + Markdown 渲染 + 落盘
│   │                      ✅ 纯计算 —— 模型不可用也照样能出晨报
│   │                      ✅ 多路会话合并进同一份「每日日报」，各路用标记分节
│   │                      ✅ v3.2.0：build_briefing_summary() —— 纯数据的手机摘要
│   │                      ✅ v3.2.0：如实标注 L2 降级次数（「那段时间漂移检测等于没开」）
│   ├── correction.py     ✅ 一键复制纠偏提示（v1.2.0）
│   │                      ✅ 红线级 →【复制拦截原因】；其余 →【复制纠偏提示】
│   │                      ✅ 提示 = 人话解释 + 错误状态 + 任务目标 + 最近几步
│   ├── injector.py       ✅ 注入中枢（v2.0.0）：stdin 直写 + 留言箱降级 + 重启注入
│   │                      ✅ InboxStore：sentinel_inbox.json（原子写；坏了当空箱）
│   │                      ✅ AgentInjector：渠道优先级 stdin → inbox（失败只降级）
│   │                      ✅ InjectionResult.next_step：给用户看的中文指引
│   │                      ✅ supports_stdin 默认 False —— 写进去没人读就不假装送达
│   ├── safepoint.py      ✅ 安全点快照与一键回滚（v2.0.0）
│   │                      ✅ capture：三重降级保「极速」（体积 / 文件数 / 时间预算）
│   │                      ✅ 清单始终完整 —— 回滚靠它判断哪些文件是新增的
│   │                      ✅ plan() 只算不动（先给用户看）；restore() 才动手
│   │                      ✅ 路径穿越防护 _inside()；排除目录（.git/node_modules）不动
│   │                      ✅ baseline()：会话起点的快照 = 默认回滚目标
│   │                      ✅ v2.1.0：capture 顺便算「相对上次变了什么」（只比清单）
│   │                      ✅ v2.1.0：diff() 按需算 unified diff（二进制自动跳过）
│   ├── resurrect.py      ✅ 一键复活编排（v2.1.0）
│   │                      ✅ 回滚 → 注入 → 结束挂起进程 → 重拉 Agent
│   │                      ✅ plan() 只算不动；每个会话一把自己的锁（并发防呆）
│   │                      ✅ 注入只走留言箱（旧进程马上要结束了）
│   │                      ✅ RESURRECT_MESSAGE：终端与看板打印同一句
│   ├── webhook_probe.py  ✅ Webhook 真实连通性自检（v2.1.0）
│   │                      ✅ 启动时真发一条（含版本号与本机 IP）
│   │                      ✅ alert.webhook_startup_test 开关（默认开）
│   ├── timeline.py       ✅ 案发时间线（v2.2.0）
│   │                      ✅ build_timeline：事件 + 告警 + 安全点 → 一条带时间戳的线
│   │                      ✅ ⚠️ 间隔（gap_text）——「隔了多久」才是用户要的那个数
│   │                      ✅ 节点关联安全点 / Diff 基线；没有就明说没有，不编
│   │                      ✅ 纯函数：只吃字典、只吐字典（看板能用，单测也能用）
│   ├── health.py         ✅ 开箱安全体检（v2.2.0）
│   │                      ✅ 四项：Ollama / 云端密钥 / Webhook / 快照排除规则
│   │                      ✅ ⚠️ 没配 Webhook = 警告 +「半夜收不到手机推送」
│   │                      ✅ 体检不烧显存不烧钱（num_predict=8 / max_tokens=4）
│   │                      ✅ 绝不抛异常（R5）；safe_print 编码降级
│   │                      ✅ v2.2.1：run_startup_health_check —— 进程级守卫
│   │                      ✅ v2.2.1：一个进程只体检一次、只打印一次
│   │                      ✅ v3.3.0：本地引擎掉线时报告尾部**跳出表格**给醒目说明
│   ├── engine_watchdog.py ✅ 本地引擎心跳看门狗（v3.3.0）
│   │                      ✅ 每 30 秒**只读** GET /models（不触发推理、不烧显存）
│   │                      ✅ 探活前**非阻塞**抢引擎串行锁 —— 抢不到就跳过这一拍
│   │                      ✅ 掉线 → [CRITICAL] 红告警；恢复 → 也报一声
│   │                      ✅ ⚠️ **绝不自动拉起引擎**（只读边界；不导入 subprocess）
│   │                      ✅ ⚠️ 只盯本机地址；云端 API 一律不探
│   │                      ✅ 同一 base_url 进程内只起一个（单例表 + 锁）
│   ├── timebox.py        ✅ 时间盒检测器（v3.3.0）
│   │                      ✅ 两种桶：同一条命令（不限类别）+ 同一类操作（限配置类别）
│   │                      ✅ 撞破 → [CRITICAL] 严重跑偏 → 通知通道推手机
│   │                      ✅ 同一件事只报一次（命令桶与类别桶同时命中时不刷屏）
│   │                      ✅ 廉价查看命令（git status 等）不计入重复统计
│   │                      ✅ 守护者联动**三态**：留空=跟着武装状态走 / true / false
│   ├── autostart_residue.py ✅ 自启残留**只读**检测（v3.3.0）
│   │                      ✅ 查「启动」文件夹快捷方式 + dist/logs 的 VBS + 注册表 Run 项
│   │                      ✅ ⚠️ **只检测不删除**（删用户系统文件是用户的决定，R7）
│   │                      ✅ render_notice：发现残留就**显眼地**提示并给清理命令
│   ├── hotreload.py      ✅ 规则热重载（v2.2.0）
│   │                      ✅ ConfigWatcher：mtime 轮询 + 去抖（一次保存只提示一次）
│   │                      ✅ RuleHotReloader：只换 RuleEngine，原子替换
│   │                      ✅ ⚠️ 坏配置保留旧规则 —— 绝不让防线清零
│   │                      ✅ ⚠️ 没用 watchdog（从来不是本项目依赖）—— 见版本文档 §4.1
│   ├── channels.py        ✅ 告警通道：终端 / 文件 / 桌面弹窗 / 声音 / Webhook
│   │                      ✅ format_remote_card：远程卡片正文（五要素）
│   │                      ✅ WebhookChannel：五种报文格式 + 加签 + 防骚扰
│   │                      ✅ v1.1.0：后台线程 + 有界队列投递，指数退避重试
│   │                      ✅ v1.1.0：4xx 不重试（重试无用），5xx/超时/429 才重试
│   │                      ✅ v2.0.0：is_notification（记录型 / 通知型）
│   │                      ✅ v2.1.0：last_response（记 HTTP 状态与响应体）
│   │                      ✅ TRIGGER_* 触发标记常量
│   │                      ✅ v3.2.0：build_briefing_payload / send_briefing（晨报摘要）
│   │                      ✅ v3.2.0：triggers 空列表 = 推全部（不静默吞告警）
│   │                      ✅ v3.2.0：close() 先 flush 再停线程（不丢队列）
│   ├── notifier.py        ✅ Notifier：等级过滤 + 逐通道分发 + 失败隔离
│   │                      ✅ _build_webhook：装配全部 Webhook 配置项
│   │                      ✅ v2.0.0：AlertCoalescer（指纹 = agent+rule_id+command）
│   │                      ✅ v2.0.0：红线级永远直通；尾随摘要 + 看门狗线程
│   │                      ✅ v3.2.0：push_briefing / flush（等真实投递结果）
│   ├── main.py            ⬜ 入口：完整生命周期（v0.8.0）
│   └── config.py          ⬜ 配置校验（v0.8.0，暂由 config_loader 承担）
├── adapters/
│   ├── base.py            ✅ BaseAdapter：read_stream 生成器 + 统一事件契约
│   │                      ✅ tail_stream：tail -f 增量读取（轮转 / 截断不崩）
│   │                      ✅ 消息容器解析（与产品无关，Claude Code / mcode 共用）
│   ├── mcode.py           ✅ McodeAdapter：thinking / tool_call / tool_result
│   │                      ✅ v0.6 事件溯源协议（item.* / toolCall / reasoning）
│   ├── codex.py           ✅ CodexAdapter：codex exec --json 的 item.* 协议
│   │                      ✅ command_execution 在 item.started 时命令已完整
│   ├── claude.py          ✅ ClaudeAdapter：Claude Code stream-json（⚠️ 未实机验证）
│   ├── registry.py        ⬜ 适配器注册表（v0.8.0）
│   └── claude_code.py     ⬜ 旧 Claude Code 日志解析（已由 claude.py 覆盖，待清理）
├── rules/
│   ├── basic.py           ✅ RuleEngine：危险命令 + 死循环（纯正则纯逻辑）
│   │                      ✅ CatastrophicDetector：红线级判定（四类）
│   ├── engine.py          ⬜ 规则注册与调度（v0.8.0）
│   └── builtin/           ⬜ 内置规则包（v0.8.0）
├── llm/
│   ├── base.py            ✅ LLMProvider 统一契约 + Explanation + CircuitBreaker
│   │                      ✅ DriftResult + evaluate_drift（降级恒为「没漂移」）
│   │                      ✅ v3.2.1：engine_lock_for —— 同一台引擎一把串行锁（单并发引擎会崩）
│   ├── ollama_client.py   ✅ OllamaClient：本地推理 + 降级 + 熔断 + 回环校验
│   ├── api_client.py      ✅ OpenAICompatibleClient：通用 API（DeepSeek / Kimi / 网关）
│   │                      ✅ v1.2.0：密钥按主机名自动识别厂商环境变量
│   │                      ✅ v1.2.0：密钥优先级（显式 > SENTINEL_LLM_API_KEY > 配置文件 api_key > 厂商变量）
│   │                      ✅ v3.2.1：is_local_endpoint —— 本机地址不算「数据出本机」
│   │                      ✅ v3.2.1：LOCAL_ENGINE_MIN_TIMEOUT —— 本机超时不低于 600 秒
│   │                      ✅ v3.2.1：chat() 按引擎地址排队（单并发引擎绝不并发）
│   │                      ✅ v3.2.1：response_format 不被支持 → 自动摘掉重试一次
│   │                      ✅ v3.2.1：_extract_content 只取最终答案（思考过程只进 debug 日志）
│   ├── router.py          ✅ ModelRouter（v1.2.0）：多级模型路由
│   │                      ✅ 红线级 / 连续漂移 → 升级到强模型
│   │                      ✅ 升级模型降级 → 自动退回主力（红线事件不能没解释）
│   │                      ✅ models_used / escalations 记账（晨报要靠它）
│   │                      ✅ v3.2.0：degraded_calls / last_degraded_reason 记账
│   │                      ✅ v3.2.0：降级**不改口径**（仍然算未漂移）—— 只负责把失效说出来
│   ├── factory.py         ✅ 按 llm.provider 挑提供方
│   │                      ✅ build_routing_provider：必要时套一层 ModelRouter
│   ├── prompts.py         ✅ SENTINEL_EXPLAIN_TEMPLATE（含防注入数据隔离条款）
│   │                      ✅ DRIFT_EVALUATE_TEMPLATE（只输出 drift / reason 两字段）
│   ├── parsing.py         ✅ 容错解析阶梯（剥围栏 → 配平 JSON → 尾逗号 → 标注式）
│   │                      ✅ parse_drift_payload（没有 drift 字段 = 失败）
│   │                      ✅ v3.2.1：strip_thinking（剥 <think> 段，含没闭合的）
│   │                      ✅ v3.2.1：extract_last_json_object（推理模型先聊后答）
│   └── redaction.py       ✅ 后端强制脱敏（送模型前 + 落盘前）
└── gui/
    ├── server.py          ✅ FastAPI + SSE 看板，只绑回环 + Origin 白名单
    │                      ✅ 守护者端点（status / intervene / override）
    │                      ✅ TaskGoal + /api/task_goal（运行中可换目标）
    │                      ✅ TailRunner：--tail 常驻引擎 + 目标热更新
    │                      ✅ SessionSpec / Session / SessionRegistry：多路会话
    │                      ✅ resolve()：严格查找（会话级 API 用，找不到 404）
    │                      ✅ 会话级 API：/api/sessions 及 /api/sessions/{id}/…
    │                      ✅ v2.0.0：inject / rollback / safe-points 端点
    │                      ✅ v2.0.0：inject_callback / rollback_callback 回调注入
    │                      ✅ v2.0.0：normalize_alert 白名单含 safe_point 字段
    │                      ✅ v2.1.0：resume / diff / resume-plan 端点
    │                      ✅ v2.1.0：resume_callback / plan_callback 回调注入
    │                      ✅ v2.1.0：_build_resume_prompt（把「为什么被拦」说全）
    │                      ✅ v2.2.0：timeline / health 端点 + /api/health
    │                      ✅ v2.2.0：EVENT_JSON 契约（与 engine.py 各持一份常量）
    │                      ✅ v2.2.0：时间线节点可挂基线（守护者缺席时也能看改动）
    │                      ✅ v3.3.2：/api/process_activity（活跃 Agent 进程感知，8 秒缓存）
    │                      ✅ v3.4.0：/api/shutdown（彻底退出的唯一出口）
    │                      ✅ v3.4.0：Session.bound_pid / process_alive / mark_exited（三态判定）
    │                      ✅ v3.4.0：_watch_process_liveness（每 10 秒巡检存活）
    │                      ✅ v3.4.0：_effective_exclude_keywords（排除清单由产品层决定）
    ├── shutdown.py      ✅ 彻底退出的统一出口（v3.4.0，叶子模块）
    │                      ✅ ShutdownCoordinator：幂等 request() + 按序跑收尾钩子
    │                      ✅ install_console_close_handler：关黑框也走同一条路
    ├── tray.py          ✅ Windows 系统托盘（v3.4.0，叶子模块）
    │                      ✅ build_tray_image：Pillow 自绘「哨」字（找不到字体画眼睛）
    │                      ✅ 菜单只有两项：【打开看板】/【彻底退出】
    │                      ✅ start_tray：守护线程（不用 run_detached —— 它会造新幽灵）
    ├── templates/         ✅ dashboard.html（含任务目标输入框）
    │                      ✅ v2.2.0：顶栏【时间线】【体检】两个按钮
    ├── static/            ✅ dashboard.css / dashboard.js（无构建链）
    │                      ✅ v2.0.0：renderAlerts 渲染签名（列表没变就不重建）
    │                      ✅ v2.0.0：alert-card__aux（只读便利按钮不被守护者禁用）
    │                      ✅ v2.1.0：openModal（自建 DOM，不用 <dialog>）
    │                      ✅ v2.1.0：renderDiffFile（绿增红删蓝块头）
    │                      ✅ v2.2.0：openTimeline / openHealth（自建 DOM）
    ├── layout_store.py    ✅ 分栏比例记忆（原子写）
    ├── density_store.py   ✅ 密度档位记忆（原子写）
    └── density.py         ✅ 密度档位定义

config/
├── config.yaml            ✅ 本地配置（已 gitignore）
└── config.example.yaml    ✅ 可入库的配置模板

tests/                     ✅ 773 条用例
```

### 7.0 仓库目录规范（v0.6.0）

仓库根目录按「**管理类置顶 + 中文语义**」组织：

```
_管理文档/文档与报告/   管理文档（架构、贡献指南、版本记录、素材）
_构建与发布/             构建与发布物（scripts/、requirements.txt）
_备份与归档/             已退出主线的历史资产（不入库）
src/ tests/ config/ logs/   工程核心
回收站/                  临时垃圾的指定去处（不入库）
README.md CHANGELOG.md pytest.ini .gitignore   入口文件
启动哨兵.bat              入口文件（Windows 一键启动器，v1.0.0 起）
```

`_构建与发布/scripts/` 下的关键脚本：

| 脚本 | 职责 |
| --- | --- |
| `run_with_sentinel.py` | 真实 Agent 管道接管（单路 / `--session-file` 多路）；**v2.0.0**：stdin 管道 + 留言箱中继 + 任务提示注入 |
| `fire_drill.py` | **事故演练**：真拉 Agent、真挂进程、真推 Webhook、真验看板 |
| `kill_moment_stress.py` | **「猎杀时刻」压力测试**（v2.0.0）：5 连 STUCK + 中途高危删除，11 项断言 |
| `demo_v2_control.py` | **v2.0.0 控制中枢演示看板**（v2.0.0）：造「Agent 把代码改乱」的现场供实拍 |
| `demo_v21_resurrect.py` | **v2.1.0 一键复活演示看板**：真的挂起一个进程，供【回滚并重试】实拍 |
| `demo_v22_timeline.py` | **v2.2.0 案发时间线演示看板**：跑真实引擎产真实告警，供【时间线】实拍 |
| `demo_v22_health.py` | **v2.2.0 开箱安全体检演示**：打印体检报告并存 UTF-8 存档 |
| `demo_v22_hotreload.py` | **v2.2.0 规则热重载演示**：改配置看热重载 +「写坏了会怎样」 |
| `verify_double_click.py` | **v2.2.1 双击验证**：不带参数启动打包版 exe，数「体检报告」出现了几次 |
| `verify_demo_banner.py` | **v2.2.2 演示模式验证**：不带参数启动打包版 exe，查终端 6 行说明 + 看板红色横幅 + 「[演示]」会话名 |
| `verify_radar_attach.py` | **v2.3.0 雷达验证**：源码跑「发现 → 挂载 → 实时红线告警」全链路（10 项）；加 `--exe` 则验打包版的资源完整性 |
| `capture_v230_pages.py` | **v2.3.0 验收截图**：起看板 + 挂两路 Agent，截主看板与独立设置页 |
| `capture_v240_pages.py` | **v2.4.0 验收脚本**：造两个「已经在跑的 Agent」（一路命令行带日志路径 → **能**反查到 PID，一路路径不在命令行里 → **反查不到**），截两种 UI 状态同框 + 手动设定目标后的 **DRIFT** 告警 + **流吞吐压测**（输出 JSON 报告） |
| `final_night_crawl.py` | **v3.2.0 终局 Night Crawl**：30 分钟真实长任务演习 —— 真拉 `mcode exec`、真注入 STUCK/DRIFT/DANGEROUS、真挂起进程、真推 Mock Webhook、真出晨报，附内存曲线与 9 项验收断言 |
| `demo_v330_watchdog_and_timebox.py` | **v3.3.0 验收演示**：**真的**把本地引擎拔掉一次（跑完拉回来）看红告警，再喂一串「死磕」事件看时间盒拦截 |
| `remove_startup_residue.ps1` | **v3.3.0 自启残留清理**：删「启动」文件夹快捷方式 + 隐藏启动器 VBS + 注册表 Run 项；`-Status` 只看不删。⚠️ **只许清理，不许新建** |
| `add_defender_exclusion.ps1` | **v3.3.0 防误杀**：把打包目录加进 Defender 排除项（需管理员权限） |
| `test_real_webhook.py` | **真实 Webhook 触达测试**（v2.1.0）：贴自己的 Token，真发一条出去 |
| `check_doc_links.py` | 文档内部链接检查 |
| `capture_dashboard.py` | 看板截图（含 `--scroll-to` 滚动定位、`--scale` 像素密度） |

完整白名单见 [`CONTRIBUTING.md`](CONTRIBUTING.md) §5.2。

---

### 7.1 守护者与分层的关系

守护者住在**告警干预层**（第 ④ 层），不侵入前三层：

```
① 采集层 → ② 规则层（判定 severity）→ ③ 模型层（解释）→ ④ 告警干预层
                                                              └─ Guardian
```

- 规则层**只负责判定**，不知道守护者的存在；
- 守护者**只对红线条目动手**，普通高危命令它一眼都不看；
- 守护者与 GUI 之间是**鸭子类型**（`status` / `intervene` / `override`），
  所以 GUI 层依然不 import 核心层 —— 与「GUI 不 import 引擎」的解耦原则一致。

---

## 8. 后续演进方向

1. **v0.2.x**：✅ 采集层与 L1 规则层落地（见
   [`v0.2.0-collect-rules.md`](versions/v0.2.0-collect-rules.md)）。
2. **v0.3.x**：✅ L2 本地模型层落地 + 旧项目资产考古（见
   [`v0.3.0-llm-audit-layer.md`](versions/v0.3.0-llm-audit-layer.md) 与
   [`v0.3.0-asset-audit.md`](versions/v0.3.0-asset-audit.md)）。
3. **v0.4.x**：✅ 看板、多通道告警、上下文感知落地（见
   [`v0.4.0-dashboard-alerting.md`](versions/v0.4.0-dashboard-alerting.md)）。
4. **v0.5.x**：✅ 仓库重组、红线级判定、守护者模式落地（见
   [`v0.5.0-guardian.md`](versions/v0.5.0-guardian.md)）。
   规则层的**路径/目标分析**已在红线判定中落地；
   剩余：Webhook 签名与重试、增量尾随采集。
5. **v0.6.x**：白名单按「命令模式」而非规则 ID 粒度；
   守护者事件独立视图；目录规范重构与通用 LLM API 接入（见
   [`v0.6.0-llm-api-directory.md`](versions/v0.6.0-llm-api-directory.md)）。
6. **v0.7.x**：✅ 实时流式监听与意图漂移检测落地（见
   [`v0.7.0-live-stream.md`](versions/v0.7.0-live-stream.md)）。
7. **v0.8.x**：✅ 异步漂移审计与真实 Agent 管道接管落地（见
   [`v0.8.0-async-drift.md`](versions/v0.8.0-async-drift.md)）。
   剩余：漂移判定的模型吞吐优化、规则层路径/目标分析。
8. **v0.9.x**：✅ L2 解释异步化与多路 Agent 接管落地（见
   [`v0.9.0-async-l2.md`](versions/v0.9.0-async-l2.md)）。
   剩余：Claude 适配器实机验证、外部 API 真机调用、漂移判定的模型吞吐优化。
9. **v1.0.0**：✅ 发布级多路审计与远程告警落地（见
   [`v1.0.0-release.md`](versions/v1.0.0-release.md)）。
   远程 Webhook 叫醒服务 + 多路会话中枢 + 事故演练 + Windows 一键启动。
   剩余：钉钉 / 飞书 / Bark 的端到端实机推送、Claude 适配器实机验证、
   外部 API 真机调用、漂移判定的模型吞吐优化。
10. **v1.0.1**：✅ 启动体验优化落地（见
    [`v1.0.1-startup-experience.md`](versions/v1.0.1-startup-experience.md)）。
    BAT 闪退修复 + ASCII 大字报 + 自动开浏览器 + 端口冲突人话提示 +
    无参数进演示模式。审查逻辑一行未动。
11. **v1.1.0**：✅ 真实并发压测、Token 审计与告警重试落地（见
    [`v1.1.0-stress-and-tokens.md`](versions/v1.1.0-stress-and-tokens.md)）。
    剩余：钉钉 / 飞书 / Bark 的真实服务端到端推送、Claude 适配器实机验证。
12. **v1.2.0**：✅ 夜间值守晨报、云端多级路由与单文件打包落地（见
    [`v1.2.0-morning-briefing-and-router.md`](versions/v1.2.0-morning-briefing-and-router.md)）。
    剩余：同上（钉钉 / 飞书 / Bark 真实服务、Claude 适配器）。
13. **v2.0.0**：✅ **终端接管与 Agent 控制中枢**落地（见
    [`v2.0.0-control-hub.md`](versions/v2.0.0-control-hub.md)）。
    注入中枢（stdin → 留言箱 → 重启注入）+ 安全点快照与一键回滚 +
    告警合并推送 + 「猎杀时刻」压力测试。
    ⚠️ **这是第一个会写被监控方工作目录的版本** ——
    红线 R1 / R2 已**如实修订**（见 §1.1），新增 R7 可逆 / R8 否决 / R9 留痕。
    剩余：支持 stdin 的交互式 Agent 适配器、目录外操作的回滚、
    钉钉 / 飞书 / Bark 真实服务、Claude 适配器实机验证。



