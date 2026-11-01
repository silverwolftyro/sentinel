"""哨兵单文件程序入口（v2.0.0）。

为什么要有它
============

v1.1.0 之前，想用哨兵得先装 Python、装依赖、clone 仓库。
这对「想半夜挂机守一下自己写的 Agent」的用户来说门槛太高。

所以 v1.2.0 提供一个**免安装**的 ``sentinel.exe``：
双击就能跑，不需要任何 Python 环境。
v2.0.0 在此基础上把它升级成**控制中枢** ——
看板可以直接给 Agent 发指令，也能一键回滚被改坏的代码。

入口做的事
==========

=========================== ==============================================
情况                        走哪条路
=========================== ==============================================
``sentinel.exe``            演示模式 —— 生成模拟日志并打开看板
``sentinel.exe --agent …``  接管真实的 AI CLI Agent（等价于
                            ``run_with_sentinel.py``）
``sentinel.exe --scan …``   命令行审查一份已有的日志
``sentinel.exe --help``     纯文本帮助
=========================== ==============================================

两种运行形态
============

* **源码运行**：``python _构建与发布/scripts/sentinel_entry.py …``
* **打包运行**：``sentinel.exe …``

打包后资源（模板 / 静态文件 / 启动器脚本）的定位方式不同，
本文件用 :func:`_bundle_path` 统一处理，**调用方无需关心**。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from typing import List, Optional

#: 版本号（三处一致：CHANGELOG / versions 文档 / Git 标签）。
#:
#: v2.1.0 起从 :mod:`src` 取 —— **只有一处**真相，
#: 免得改了这里忘了改看板的 FastAPI 版本。
try:
    from src import __version__  # type: ignore
except Exception:  # noqa: BLE001 —— 打包解包顺序异常时的兜底
    __version__ = "4.3.1"

try:
    # v3.8.0：第二个实例的退出码 —— 与「正常退出（0）」「启动失败（2）」区分。
    from src.gui.single_instance import (  # type: ignore
        ALREADY_RUNNING_EXIT_CODE,
    )
except Exception:  # noqa: BLE001 —— 拿不到就用同样的值
    ALREADY_RUNNING_EXIT_CODE = 3

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

#: 纯文本帮助。
HELP_TEXT = """哨兵 Sentinel v{version} —— AI CLI Agent 的只读审计监控

用法：
  sentinel                          演示模式：生成模拟日志并打开看板
                                    （先看看它长什么样）
                                    ⚠️ 看板会挂红色横幅、会话名带「[演示]」，
                                       终端也会打印醒目说明 —— 都是模拟数据，
                                       没有监控任何真实进程。
                                    横幅最右侧有【退出演示，开始真实扫描】按钮，
                                    点它会停掉演示并给出真命令。
  sentinel --agent <类型> <任务>     接管真实的 AI CLI Agent，边跑边审
  sentinel --session-file <JSON>    多路会话：同时接管多个 Agent
  sentinel --scan <日志>            命令行审查一份已有的 Agent 日志
  sentinel --demo                   与无参数相同（演示模式）
  sentinel --version                显示版本号
  sentinel --help                   显示这份帮助

常用参数：
  --agent <类型>                    要接管的 Agent 类型（默认 mcode）
                                  除 mcode / codex / claude 外，
                                  还支持 14 个上游 Agent CLI
                                  （gemini / command-code / dsh / pi / step …），
                                  清单见 src/agents/cloud_agents.json
  --cwd <目录>                   Agent 的工作目录（默认当前目录）
  --port <端口>                  看板端口（默认 8000）
  --host <地址>                  看板监听地址（默认 127.0.0.1）
  --no-browser                   看板启动后不自动打开浏览器
  --no-gui                      不启动看板，只在终端审查
  --no-llm                      不调用模型层，只做规则层判定
  --no-guardian                 不启用守护者（只观察，不挂起进程）
  --no-notify                   不装配任何告警通道
  --keep-alive                  Agent 跑完后不关看板（适合挂一整夜）
  --no-briefing                 会话结束时不生成晨报
  --no-health                   启动时不跑开箱安全体检
  --config <路径>                哨兵配置文件（默认 config/config.yaml）
  --extra-arg <参数>            透传给 Agent 的额外参数（可重复）

配置与密钥：
  配置文件默认读「exe 同目录下的 config/config.yaml」；
  第一次运行会自动从模板生成一份，可以直接改。
  云端模型密钥请设环境变量（不要写进配置文件）：
    SENTINEL_LLM_API_KEY  —— 通用
    DEEPSEEK_API_KEY / MOONSHOT_API_KEY / …… —— 按厂商自动识别

v2.2.0 新增：
  【时间线】  看板顶栏 —— 把这一路发生过的事排成一条带时间戳的线
              （开始目标 / 首次意图漂移 / 红线拦截 / 复活重试 …），
              节点能直接关联到当时的「安全点」与「Diff 清单」。
  【体检】    看板顶栏 —— 开箱安全体检：本地模型通不通、云端密钥有没有效、
              手机推送（Webhook）配没配、工作目录有没有排除规则。
              没配 Webhook 会明确警告「半夜跑挂，您将收不到手机推送！」。
              启动时也会自动跑一遍并把报告打到终端。
  规则热重载  改了 config.yaml 不用重启 —— 后台自动重新加载规则，
              终端打印：[SENTINEL] 检测到规则更新，热重载完成。
              写坏了会保留旧规则并报错，不会让防线清零。

v2.2.1 新增：
  双击不闪退  无参数启动直接进演示模式 + 自动打开浏览器；
              万一出了致命错误，窗口**不会自己关**（会挂住让你看清报错）。

v2.2.2 新增：
  演示模式说破  演示里的告警 / 日志 / Token 全是**模拟数据**。
              看板顶栏挂一整条红色横幅、背景铺红色斜条纹、
              会话名变成「[演示] 默认会话」；
              终端大字报之后紧跟 6 行醒目说明，并告诉你怎么用真的。
              真实接管模式（--agent）**不会**出现这些标识。

v2.3.0 新增：
  雷达挂载      哨兵不再要求「必须从我这儿启动 Agent」。
              看板左侧点【＋ 发现/挂载进程】，
              它会扫出你**已经在跑**的 Codex / MCode / Claude 会话日志，
              点「接入」就实时审查 —— 你的 Agent 该怎么跑还怎么跑。
              认得出来时会话名会写成「[Codex] D:\\work\\project_alpha」。
  独立设置页    顶栏点【⚙ 设置】进入**独立全屏**页面（/settings）：
              模型路由 / 通知通道 / L1 规则 / 挂载管理，
              改完点【保存并应用】—— 写回 config.yaml 并立刻热重载。
              配置文件里的中文注释**全部保留**。

v2.4.0 新增：
  武装守护者    挂载进来的会话现在会**反查**出「谁在写这份日志」：
                查得到就在卡片上写明 PID，并给一个【武装守护者】开关；
                **只有你亲手打开它**，守护者才会真的接管那个进程。
                查不到就明写「[未绑定进程]」并禁用开关
                （**只是没法挂起进程**，告警与推送照常 ——
                 v3.3.1 起悬停会说明为什么、怎么办）。
  意图锚定      挂载进来的日志往往没有最初那句 Prompt，
                选中它就能在顶部「任务目标」里**手动注入**目标；
                设定之后**立刻**开启意图漂移（DRIFT）监控。

v3.5.0 新增：
  多路隔离      多个 Agent 往**同一个**日志文件里写时，哨兵按
                **线程指纹**（thread_id / session_id / 工作目录）分流：
                A 路的日志只进 A 路的滑动窗口，B 路的进 B 路的 ——
                绝不会串台。左侧同一份日志会拆成多张**虚拟会话卡片**，
                点一张就只看那一路。
  级联审核      L1 正则 → L2 本地模型（极速过滤）→ L3 云端模型（终极裁判）：
                L2 判「正常」的事件被**静默丢弃**，省下一次云端调用；
                L2 报警才叫 L3 复审。**只有 L2 与 L3 同时判定有问题，
                才亮最高级红灯**（宁可漏报也不误报）。
                设置页【模型路由】里分成「L2 本地」与「L3 云端」两个配置区。
  L2 极速优化   送给本地模型的 Prompt 会**剥掉时间戳 / [TAIL] #123 / UUID**，
                静态系统提示与事实卡片前置 —— 100% 吃满 Prefix Cache；
                请求体带 reasoning_effort: none + JSON 模式 + max_tokens 限制；
                超时压到 10 秒，429 / 503 / 超时一律回退 L1 原始判定，
                **绝不堵住主队列**。
  记忆管理      每个线程只保留最近 3-5 步的完整原文
                （Tool Call + Result 作为原子对，不可拆分）；
                被挤出的事件攒够 10 条就在**后台**做一次滚动摘要，
                摘要作为「历史背景」放在 Prompt 开头。
  省下多少算力  看板顶部多一行轻量计数器：
                「L2 已过滤 X 次冗余，为您节省 Y 次云端调用」。
  ⚠️ 级联是增强  llm.cascade.enabled 默认**关闭** ——
                  关着的时候行为与 v3.4.0 **一字不差**。

v3.5.1 新增：
  日志跟随锁定  右侧「被监控日志」不再**强制**拉到底部：
                你在底部时新日志自动跟随；用滚轮往上一翻就**立刻解除跟随**，
                你可以安安静静回看历史，不会被新日志拽走。
                右下角浮着【↓ 回到最新】，不在底部时它会高亮 ——
                点一下平滑滚到底，并**恢复跟随**。
  自然语言流水  中间「实时审核流水」不再打印机器码：
                [15:32] Agent 正在思考：关键发现是...
                [15:33] 正常执行命令：pytest
                [15:35] 警告：连续 3 次读取同一个文件，疑似死循环: core/auth.py
                [15:36] 危险！拦截到高危命令：rm -rf /
                正常=**绿色**、可疑=**橘黄**、危险=**红色**；
                摘要取自事件的 thinking / tool_call 前 40 个字符，
                不会把原始 JSON 糊到脸上。
  空态引导      雷达扫不到任何在跑的 Agent 时，不再只写「未发现」：
                直接给出可执行的命令，并附一个【复制命令】按钮，
                复制完粘到终端就能开始监控。
  状态呼吸灯    顶部状态标签旁多一颗**呼吸灯**：
                引擎在跑就一直跳动，暂停或停止就变成灰色常亮 ——
                一眼看出系统是不是活着。

v4.0.1 新增：
  首发不卡了    进程树雷达**第一次扫**不再卡住界面：
                服务启动时就在后台**静默预热**进程枚举，
                预热完成后你再点开雷达，扫描是**几十毫秒**的事。
                冷启动那几秒的耗时全部挪到了启动期。
  扫久了会退场  万一某次扫描超过 1.5 秒还没扫完，
                界面**不等**它 —— 先把已经扫到的部分显示出来，
                标题栏提示「正在深度检索进程…」，
                扫完自动补齐。**绝不卡住主线程**。
  偏航基线重跑  判定用的「演进流」换了新格式，
                旧格式（滑窗摘要）当对照、新格式（压缩演进流）当实验，
                用同一批真实窗口、同一个模型、同一个目标、
                只换摘要生成方式，两个格式**逐窗对比**。
                结论写在 CHANGELOG 与版本文档里，**数字都是实跑出来的**。

v4.3.1 新增：
  界面中性化    雷达面板的标题从某个特定项目的私人称呼，
                改成中性的「检测到的外部进程」。
                徽章、日志提示、设置页示例、示例配置、
                --help 里都不再出现任何特定项目名。
  开源致谢      新增根目录 NOTICE 文件，说明本项目参考并使用了
                addsas222/n.e.k.o_plugin_agent_bridge 的部分
                数据结构与实现思路（该项目为 MIT 协议），
                README 底部也加了致谢段与链接。

v4.3.0 新增：
  已知日志目录  Agent CLI 常常不往自己的工作目录写日志：
                Claude Code 写 ~/.claude/projects/，
                Codex 写 ~/.codex/sessions/。
                雷达以前只在工作目录下找，找不到就把进程
                标成「疑似挂起」—— 那是**假阳性**。
                现在清单里登记了已知会话日志目录，
                工作目录没有就再去那里找。
  实测外部插件接管  真起了上游插件的控制台，通过它派发了一个
                OpenCode 任务：Agent 真的跑完了，
                雷达也扫到了那个子进程（名字、PID、
                工作目录都对），日志是在工作目录之外找到的。

v4.2.0 新增：
  疑似挂起      进程启动后 **10 秒**还没吐出任何输出，
                雷达面板不再显示「运行中」，
                改成灰色徽章「慢/待验证 · 疑似挂起」，
                tooltip 把三种可能都列出来。
                ⚠️ 10 秒只能说明「还没动静」，
                不能断言它挂了 —— 所以用灰标不用警示色。

v4.1.0 新增：
  Agent 数据化    「怎么启动一个 Agent CLI」彻底变成**数据**：
                14 个上游 Agent 的可执行名、启动形态、参数模板、
                鉴权措辞、登录指引全部登记在
                src/agents/cloud_agents.json 里。
                **新增一个 Agent = 加一条 JSON，不用改代码。**
  别名补齐      此前雷达只认得出 8 个别名，
                gemini（gcli）、command-code（cmdc）、
                step（stepcode）、pi（pi-coding-agent）等
                **8 条别名一概认不出** —— 现在全部归一。
  认出没登录    这些 CLI 未登录时**退出码往往是 0**，
                只往 stderr 打一行登录提示 ——
                以前会被当成「任务完成」。
                现在按措辞认出来，明确告诉你「这台没登录」。
  慢/待验证     进程活着却一直没输出，雷达面板**单独标一档**，
                不再让你对着一个空转的进程干等。
  列表不跳动    会话列表与实时流水区改成「内容没变就不重建」，
                轮询不再把你正在看的条目抽走。

v4.0.0 新增：
  进程树雷达    第三方工具派发出去的 Agent 进程，
                现在哨兵能**自己扫出来**：
                先找到桥接宿主（python -m webui.server
                或跑在插件数据目录下的进程），
                再枚举它的**全部子孙进程**，
                按命令行特征认人、读工作目录，
                关联目录下的日志文件。
                认出来就显示成「已发现但在运行中」，
                关联到真实日志就能**一键挂载**。
                ⚠️ 不需要对方改任何代码。
  摘要先压缩    判定偏航之前，先把事件序列压成高信噪比的
                「演进流」再送模型 ——
                不再粗暴截断尾巴，
                而是留下步号、工具名、命令文本和当前动作。
  微统计打断    反复读同一个文件、反复跑同一条命令这种**空转**，
                现在是独立的前置检测器：
                命中就打断后续判定、当场生成告警，
                不再干等模型回话。
  红线插队      本地模型的判定请求现在排队：
                红线（P0）插到队首，危险动作（P1）次之，
                常规判定（P2）垫底。
                ⚠️ 抢占会**真的丢掉一次模型调用**（计费成本）。

v3.9.0 新增：
  常规不判偏航  改 CHANGELOG.md / package.json / version.txt、
                跑 pytest / npm run build 这类**常规铺垫**
                **不再**被判成「跑偏」——
                这类事实是字面可判定的，
                哨兵直接判「正常」，**根本不问模型**。
  摘要只留人话  流水摘要里的 ``` 代码块**整段剥掉**；
                滑条拉到 100 档只留自然语言描述，
                长文本**只取前两句**。
  短输出不截断  只要求「回答一个字」的请求，
                现在 max_tokens 至少 50 且强制关掉思考，
                不会再被推理内容把额度吃光、正文一个字都吐不出来。
  手机也能用    窄屏断点统一到 768px，底部加了四个 Tab
                （告警 / 流水 / 日志 / 设置），
                按钮最小 44px，窄屏不再显示拖不动的分隔条。

v3.8.0 新增：
  单例锁        **双击两次不会再出现两个托盘图标**了：
                哨兵启动时会在 Windows 上拿一把命名互斥体，
                拿不到（说明已有人在跑）就弹一句
                「哨兵已在运行中」并**优雅退出**（退出码 3），
                绝不开第二个看板、第二个 Agent。
                ⚠️ 终端里也会打印同一句话 —— 认得出来。
  进程同生共死  哨兵把自己放进 Windows Job Object
                （KILL_ON_JOB_CLOSE）：**主进程一死，子进程跟着死**，
                不再留下关不掉的孤儿进程。
                ⚠️ 只管哨兵**自己拉起**的进程 ——
                雷达挂载进来的、属于你的 Agent，**绝不**去杀（R6）。
  L2 掉线热切   本地模型（NInfer）突然掉线时，不再干瞪眼：
                **临时性**掉线（连不上 / 429 / 503 / 超时 / 熔断中）
                会把**原本给 L2 的载荷**直接转给云端 L3，
                并在流水里标注：
                「[WARN] L2 掉线，已自动切换至 L3 深度审查」。
                ⚠️ 热切是**单盲**（只有 L3 一票），所以**绝不亮红灯**。
  L3 预算熔断   热切是要花钱的。设置页可以设「L3 回退预算」
                （窗口内次数上限 / 预估金额上限）：
                超阈值就**放弃**这次云端调用、回退 L1 纯规则，
                并发一条**严重系统告警**。
                ⚠️ 预算是**估算值**，实际账单以服务商为准。
                L3 也挂了就**安静回退 L1** —— 不报错、不卡死。

v3.7.0 新增：
  规则管理页    设置里多了一个「L1 规则管理」子页（/settings/rules）：
                23 条内置规则（18 高危 + 4 红线 + 1 死循环）列成表格，
                每条一个 **Toggle** —— 可以关掉，也可以从「拦截」
                降级成「仅记录」（命中照样出告警，但**不挂起**进程）；
                还能整份 **JSON / YAML 导入导出**（导出即无损备份，
                导回时开关状态一并还原）。
  流水线控制    设置里多了一个「流水线控制」子页（/settings/pipeline）：
                **L1 / L2 / L3 各自独立开关** ——
                显存不够就把 L2 关掉（关掉之后 L2 **根本不装配**）；
                再打开「L2 → L3 桥接」，**L1 没命中**的事件会直接
                送给云端 L3 复核。
                ⚠️ 桥接是**单盲**（只有 L3 一票），所以永远**不亮红灯**，
                   只出标准级告警、**绝不挂起**。
                同一页还能选「L1 命中后怎么办」：
                只拦红线级（默认）/ 直接挂起 / 只告警不挂起 /
                放行给 L2 二次判断。
  极限摘要      「呈现与摘要」卡片有两根 0-100 滑条
                （右侧被监控日志 / 中间实时审核流水）。
                拉满到 **100**，右侧日志会**真的合并**：
                连续 5 次读文件 → 「[读取] core/auth.py 等 5 个文件」；
                中间流水在 90 以上**省略思考过程**，只留结论与危险动作。
                ⚠️ 主看板上的档位滑条已**移走**（看板只留监控 / 告警 / 会话）。

v3.6.1 新增：
  上下拖拽修正  中间「实时审核流水 / 实时告警」之间的横向分隔条
                **不再反向**：鼠标往下拖，分隔条**跟着往下走**，
                流水区向下长大、告警区缩小；往上推则反过来。
                ⚠️ 根因**不是** Y 轴算反了（`startAudit + dy` 的符号一直是对的），
                   是把手挂在了「被面板底边钉死、动不了」的那条边上 ——
                   流水区挪到分隔条上方之后，
                   把手与左右两条分隔条**完全同构**。

v3.6.0 新增：
  无极调节渲染  右侧「被监控日志」顶部多一根 0-100 的**无极滑条**：
                0-30 是**原始 JSON**（带语法高亮），
                30-70 是**自然语言**（🧑 用户提问 / 🤖 Agent 回答 / 💭 思考，
                自动藏掉 arguments 与 tool_result），
                70-100 是**高度摘要**（只留问题 + 回答 + 核心工具调用）；
                拖动时从缓存重绘，**不闪**。
  面板自由拖拽  左侧会话栏与右侧日志可**左右拖**改宽度，
                中间「实时告警 / 实时审核流水」之间可**上下拖**；
                比例存浏览器本地，**刷新后还在**。
  会话栏可折叠  左侧会话栏能收成一条**极窄侧边条**（只显示会话数），
                点一下就展开。
  接入前校验    手动输入路径点【接入】时，**先问后端文件在不在**：
                不在就直接飘红字「文件不存在，请检查路径或重新生成」，
                **不再甩 500 报错**。
  空态不装懂    雷达空态给的是**相对路径** logs/capture.jsonl，
                并提示「请先运行该命令，生成文件后再点击接入」——
                **绝不假装文件已经就绪**。
  流水问答高亮  实时审核流水里的 user_message / text
                改用**气泡样式**突出，一眼看出哪句是核心上下文。

v3.5.2 新增：
  界面不再重叠  中间面板**彻底改成 Flex 纵向布局**：
                实时告警与实时审核流水各占各的一行、自己滚，
                **不再有任何一个绝对定位或 z-index 穿透** ——
                文字叠在一起的灾难级问题已修。
                （⚠️ 两段的上下顺序在 v3.6.1 调换过，
                  原因见上面「v3.6.1 新增」。）
  自动任务锚点  雷达挂载会话时**自动去日志里找任务目标**：
                先读文件前 10 行，找第一条真正的用户消息；
                mcode 这类命令行里带 Prompt 的，再从进程 cmdline 里取；
                系统噪声（<system-reminder> 之类）会被**过滤掉** ——
                宁可找不到，也不能找错。
                所有手段都失败，才保留「请手动输入」。

v3.4.0 新增：
  系统托盘      哨兵在 Windows 右下角常驻一个图标：
                  右键菜单只有两项 ——【打开看板】与【彻底退出】。
                  【彻底退出】会**优雅地**关掉看板服务、结束哨兵**自己拉起**的
                  子进程、收掉图标，然后退出程序 —— **不留任何后台残留**。
                  关掉黑框（CTRL_CLOSE_EVENT）也走同一条路。
  关闭哨兵      看板右上角多了一个**红色**按钮【关闭哨兵】：
                  点一下会先弹确认框（列清三条后果），确认后后台彻底退出。
  卸载会话      左侧每个会话卡片多了一个 [×]：
                  点它就停止跟随那份日志，列表里立刻消失。
                  雷达自己那个占位会话**不给**这个按钮。
  状态纠偏      挂载时反查到了 PID 的会话，每 10 秒查一次存活：
                  进程没了就把状态从「运行中」改成「已退出（静默）」，
                  并且**停止重放**那份不会再变动的旧日志。
  雷达排除自身  设置页可以配 radar.exclude_keywords（默认 sentinel/stress/test），
                  路径命中这些词的日志**一律不显示** —— 哨兵自己的压测残留
                  不再挤掉你真正在跑的任务。
  自定义特征    设置页可以配 radar.custom_process_features（例如 my-cli）：
                  命令行**或工作目录**命中它的进程，雷达也会认出来并提示。
  旁路说明      设置页写清楚了：哨兵是**旁路日志跟随**，不是侵入式 Hook；
                  前提是 Agent 开了 JSON 日志输出。
  ⚠️ 子进程边界  退出时**只**结束哨兵自己拉起的子进程；
                  雷达挂载进来的、属于你的 Agent，**绝不**去动。

v3.3.2 新增：
  活跃进程感知  雷达除了扫**文件**，还扫**进程** ——
                发现「本机有正在跑的 Agent，却对不上正在监控的日志」时，
                看板左侧会亮起橘黄提示：
                「检测到本机有 1 个活跃的 MCode 进程，未发现可读日志。」
                点它就能直接去填日志路径（或用 `> log.jsonl` 重定向重启）。
                ⚠️ 一切正常时它**不占地方**。
  快捷重定向    设置页【挂载管理】里多一块**可复制的启动命令**：
                照着跑，Agent 的输出就落到哨兵认得的文件上，
                下次开任务不用再想「日志到底在哪儿」。
                换 Agent 只需在下拉框里选，命令会跟着变。

v3.3.1 新增：
  实时审核流水  中间「实时告警」面板的**底部**多了一块流水区 ——
                L1 每判定一次正常，就留一行极简灰字：
                「[12:01:05] 正常读取: auth.py」。
                不显示完整 JSON，只显示意图摘要。
                它是**背景层** —— 一旦触发告警，告警卡片盖在它上面。
  无目标强引导  挂载进来的会话没有初始 Prompt 时，
                顶部那一句「未设置」会变成**橘黄高亮**，
                旁边挂一个气泡告诉你该往哪儿填；
                点一下它，光标直接送到目标输入框。
                设定之后，顶部变成「[MCode] 手动锚定」。
  进程反查宽容  查不到 PID 时，界面会把话说清楚：
                「未找到对应进程。如需拦截，请使用 --agent 模式启动，
                或确保 Agent 命令行包含日志路径。」
                ⚠️ **L1/L2 告警与推送不受影响** —— 只是无法挂起进程。

v3.3.0 新增：
  无自启        **彻底删除**了「开机自启」与「守护模式」——
                哨兵**只在你手动启动时**才运行，用完即走。
                旧版留在「启动」文件夹里的快捷方式会被**当场检测并提醒**清理。
  引擎心跳      本地模型引擎掉线时，哨兵会**立刻**红色告警：
                「[CRITICAL] NInfer 引擎已掉线！L2 判定和解释已停用！」
                每 30 秒只读探活一次，不烧显存、不触发推理；
                引擎回来了也会报一声。配了 Webhook 的，手机上也会响。
                ⚠️ **只监控，不自动拉起** —— 保持只读边界。
  时间盒        「一个子任务吃掉整轮」现在会被**硬性拦截**：
                同一件事（或同一类操作）磨过时间盒（默认 15 分钟），
                直接触发 [CRITICAL] 严重跑偏 并推送到手机。
                守护者武装着的时候，会**自动挂起**死磕的进程（可逆）。
  防误杀指南    Defender 可能把单文件 exe 误判成木马并隔离 ——
                **手动双击**打不开的话，把 exe 所在目录加入排除项。

v3.2.1 新增：
  本地 NInfer    全面适配本机 NInfer（Bonsai2-27B）审核引擎：
                128K 上下文、单并发、超时拉满 600 秒。
                ⚠️ 单并发 = 同时发两个请求**引擎会崩** ——
                哨兵已在客户端排好队（同一台引擎一把锁）。
  思考剥离      推理模型的思考过程**一个字都不进**看板告警文案 ——
                只取最终答案，思考只进 debug 日志。
  退让重试      引擎不认 response_format（发过去直接 400）时，
                自动摘掉字段重试一次，并记住 —— 不再白跑。
  本机不外送    引擎地址在本机（127.0.0.1）时，
                **不会**再喊「数据会离开本机」—— 那是假话。

v3.2.0 新增：
  晨报投递      晨报生成完之后**自动**推到你的手机（钉钉 / 飞书 / Server酱 / Bark）。
                推的是**提炼过的核心数据**（总拦截次数 / 红线次数 / 挽回 Token），
                不会因为太长被手机截断。
                ⚠️ 晨报推送**不受**「触发类型」与「防骚扰窗口」影响 ——
                否则你配了 triggers: [DANGEROUS] 的话，晨报永远推不出去。
  关于系统      设置页第 ⑤ 项 —— 运行时长、内存占用、版本号一眼可见。
                「监控工具本身不能变成资源消耗大户」，所以它得自己量自己。
  内存防线      被跟随的日志写出超长行（迟迟不换行）时，
                跟读器的缓冲有 1 MiB 硬上限，超了丢最老残片 —— 不会变成内存黑洞。
  ⚠️ 触发类型清空 = 全都要
                设置页里把 triggers 列表清空，意思是「我全都要」，
                不是「都别推」—— 哨兵**不会**静默吞掉你的告警。

v3.1.0 新增：
  挂载持久化    挂载过的会话会记进「config/attached_sessions.json」，
                哨兵重启（崩溃 / 更新 / 机器重启）之后**自动接回来**，
                不用再点一遍。日志文件没了、或者静默超过 1 小时的，
                会被自动清理掉（终端会打印是哪一条）。
  重定向指引    压缩格式（zstd）的日志接不进来 —— 雷达里点那个橙色按钮
                会直接告诉你「怎么才行」：
                启动 Agent 时加输出重定向，再从【手动指定日志路径】接入。
                设置页【挂载管理】里还有一块「如何让我的 Agent 被监控」，
                列出 mcode / codex / claude / deepseek 各自的启动命令。
  ⚠️ 已删除    v3.3.0 起**不再有**「守护模式」与「开机自启」——
                哨兵**只在你手动启动时**才运行，用完即走。

v3.0.2 新增：
  启动菜单      双击 exe **不再直接进演示** —— 终端里先问你：
                  [1] 启动演示模式  [2] 开启雷达  [3] 接管并启动新的 Agent
                直接回车 = [2] 开启雷达（默认给「真实的东西」比给「假的」诚实）。
                非交互环境（CI / 后台服务）不阻塞，直接用默认值并说明选了哪个。
  路由级设置页  设置页是**真正的多页面应用**：
                  /settings 是干净的菜单列表，
                  /settings/llm、/settings/webhook、/settings/rules、
                  /settings/sessions 各管各的，**URL 会真的变**。
  精准雷达      雷达默认**只报正在写入的**会话（静默文件连读都不读，快 200 倍）；
                .codex 降到最低优先级，.minimax/v2/sessions 与 .dsh/sessions 优先；
                DeepSeek harness 的 zstd 压缩日志也能读出工作区与会话 ID
                （⚠️ 压缩流没法按字节跟读，所以只支持「发现」，不支持「接入」）。
  启动命令说明  设置页【挂载管理】里给出一条**能一键复制**的命令，
                告诉你怎么开一个雷达能准确定位到的 Agent 任务。

v3.0.1 新增：
  退出演示      演示模式的红色横幅最右侧有个大按钮【退出演示，开始真实扫描】——
                点它会**真的**停掉演示进程，并留下能直接复制的真命令。
  设置分栏      设置页改成标准后台布局：左边 240px 导航（点一下切右边），
                卡片不再自带小滚动条，窄屏下导航变顶部横向 Tab。
  雷达工作区    雷达现在能挖出**真正的工作区**（不是「未知工作区」），
                并显示会话 ID 与首条消息时间；
                扫描哪些目录由 config.yaml 的 radar.scan_roots 决定。

v3.0.0 新增：
  自动锚定      挂载进来的日志**自带**最初那句 Prompt 时，哨兵会**自动**
                把它设为这一路的任务目标，卡片上标【自动提取】——
                你不用再手打一遍。
  武装建议      反查置信度够高（≥0.95）时，【武装守护者】开关会**高亮**
                并给一句气泡提示。**它只建议，不会替你开** ——
                挂起别人的进程永远要你亲手点。
  手机可用      看板与设置页都做了**窄屏适配**，手机上直接开链接就能看。
  启动问候      终端上会打一行：Sentinel v{version} is ready.

安全边界：
  哨兵**绝不替 Agent 改代码**。
  守护者模式下最多把进程**挂起**，而且随时可以放行，所有动作都留痕在 logs/。
  意图漂移与死循环**只告警、不挂起** —— 那不是红线。
  v2.0.0 起有三件事需要你**点头**才会发生：
    · 【直接发送给 Agent】—— 往 Agent 的标准输入写一条纠偏提示；
      CLI 不读 stdin 时降级为留言箱（sentinel_inbox.json），下次启动时注入。
    · 【一键回滚至拦截点】—— 先给出「恢复 N 个 / 删除 M 个」的计划，
      你确认后才恢复工作目录里的文件。
    · 【回滚并重试】—— 回滚 + 注入纠偏提示 + 结束那个**被哨兵挂起**的进程
      + 重新拉起 Agent。这三步不可逆，所以也**先出计划、你点头才动手**。
      ⚠️ 这是「哨兵绝不 kill 进程」的**唯一例外**，且只对
      哨兵自己挂起过的进程生效。
  这些事都可逆（除最后那条的进程终止）、都留痕、都能被你否决。
"""


def _bundle_path(*parts: str) -> str:
    """定位「随包文件」（模板 / 静态资源 / 启动器脚本）。

    * 源码运行：走项目里的真实路径；
    * 打包运行：走 PyInstaller 的解包目录 ``sys._MEIPASS``。

    :param parts: 相对路径片段。
    :return: 绝对路径。
    """
    bundle = getattr(sys, "_MEIPASS", "") or ""
    if bundle:
        candidate = os.path.join(bundle, *parts)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(_ROOT, *parts)


def _prepare_stdout() -> None:
    """让「重定向 / 管道」场景下的中文输出不乱码。

    Windows 控制台本身走 UTF-16 通道，中文天然正常；
    但一旦 stdout 被重定向到文件或管道，Python 会退回系统 ANSI 代码页，
    中文就变成乱码。这里**只在非交互场景**切换编码，不影响真实终端。

    （与 :func:`src.core.engine._prepare_stdout` 同一套做法。）
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if not stream.isatty():
                reconfigure(
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
        except (ValueError, OSError, AttributeError):
            pass


def _ensure_user_config() -> str:
    """打包运行时，在 **exe 旁边**准备好一份可编辑的配置文件。

    用户拿到的是「一个 exe + 一个 config 目录」——
    配置文件必须是**他改得动**的，而不是躺在临时解包目录里。

    :return: 实际使用的配置文件路径；没准备出来时返回空串。
    """
    if not getattr(sys, "frozen", False):
        return ""
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    target_dir = os.path.join(app_dir, "config")
    target = os.path.join(target_dir, "config.yaml")
    if os.path.exists(target):
        return target
    template = _bundle_path("config", "config.example.yaml")
    if not os.path.exists(template):
        return ""
    try:
        os.makedirs(target_dir, exist_ok=True)
        shutil.copyfile(template, target)
    except OSError as exc:  # noqa: BLE001 —— 写不了就退回只读运行
        print(f"[SENTINEL] 没能生成配置文件（{exc}），将使用随包默认配置。")
        return ""
    print(f"[SENTINEL] 已在 {target_dir} 生成配置文件，可直接编辑。")
    return target


def _ensure_project_root() -> None:
    """把项目根塞进 ``sys.path``（打包运行时是必要的）。

    * **源码运行**：项目根就是仓库根；
    * **打包运行**：没有「项目根」这回事 —— 用 **exe 所在目录**。
      配置、日志、晨报归档都该落在用户看得见、删得掉的地方，
      而不是 PyInstaller 的临时解包目录（那东西一关就没了）。
    """
    if getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = _ROOT
    if root and os.path.isdir(root) and root not in sys.path:
        sys.path.insert(0, root)


def _load_launcher():
    """加载「真实管道接管」启动器模块。

    打包后它是一个**数据文件**（不是可导入模块），
    所以用 :mod:`importlib` 按路径加载。

    :return: 模块对象。
    """
    _ensure_project_root()
    path = _bundle_path("_构建与发布", "scripts", "run_with_sentinel.py")
    spec = importlib.util.spec_from_file_location("sentinel_launcher", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"找不到启动器脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sentinel_launcher"] = module
    spec.loader.exec_module(module)
    return module


def _print_version() -> int:
    """打印版本号。

    :return: 退出码。
    """
    print(f"哨兵 Sentinel v{__version__}")
    return 0


def _warn_autostart_residue() -> None:
    """启动时检查**旧版留下的开机自启残留**（v3.3.0）。

    哨兵 v3.1.0 ~ v3.2.1 有「开机自启」功能，v3.3.0 **彻底删除**了它。
    但**已经装过的机器上还有残留** ——
    「启动」文件夹里的快捷方式、隐藏启动器 VBS。

    不清理的话，下次开机哨兵照样会被**静默**拉起来，
    而用户以为早就删干净了。

    ⚠️ 这里**只提示、不删除** ——
    删用户系统里的文件是**用户的决定**。
    真要删，跑 ``remove_startup_residue.ps1``。

    :return: 无。
    """
    try:
        from src.core.autostart_residue import render_notice

        notice = render_notice()
    except Exception as exc:  # noqa: BLE001 —— 检测失败不影响启动
        print(
            "[SENTINEL] 自启残留检测跳过（{}: {}）。".format(
                type(exc).__name__, exc
            ),
            flush=True,
        )
        return
    if notice:
        print(notice, flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    """统一入口。

    ⚠️ 这里包了**最外层的守卫**（v2.2.1）——
    双击 exe 时一旦出了致命错误，窗口会**瞬间消失**，
    用户连报错都来不及看，那比不做功能还糟糕。
    所以出错时先说清楚，再**挂住**等用户看完。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码。
    """
    _ensure_project_root()
    _warn_autostart_residue()
    try:
        from src.console import fatal_guard
    except Exception as exc:  # noqa: BLE001 —— 连提示模块都导不进来
        print(
            "[SENTINEL] 哨兵没能启动（{}: {}）。".format(
                type(exc).__name__, exc
            ),
            flush=True,
        )
        _wait_for_user()
        return 1
    # ⚠️ v3.3.0：出错时**一律**挂住等用户看完 ——
    #    旧版的「守护模式不挂住」逻辑已经删掉了：
    #    哨兵不再有隐藏窗口运行的路径。
    return fatal_guard(
        _dispatch, argv, wait=True
    )


def _wait_for_user(message: str = "程序发生错误，按回车键退出……") -> None:
    """出错后**挂住**等用户看完。

    ⚠️ 只有在**真的连着终端**时才等；
    被管道 / 脚本调用时不能把调用方挂住。

    :param message: 等待提示语。
    """
    try:
        from src.console import wait_for_user

        wait_for_user(message)
    except Exception as exc:  # noqa: BLE001 —— 连提示模块都没有就算了
        print(
            "显示等待提示失败：{}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
        )


def _dispatch_takeover() -> int:
    """接管并启动新的 Agent（启动菜单的选项 3，v3.0.2）。

    先问「接管哪个 Agent」，再问「要它干什么」——
    问清楚了才动手，用户不用去翻文档。

    :return: 进程退出码。
    """
    agent = _ask_agent_choice()
    task = _ask_task()
    if not task:
        print(
            "[SENTINEL] 没有任务描述就没法启动。"
            "也可以改成命令行直接跑：sentinel --agent {} \"你的任务\"".format(
                agent
            )
        )
        return 2
    launcher = _load_launcher()
    return int(launcher.main(["--agent", agent, task]) or 0)


def _ask_agent_choice() -> str:
    """问用户要接管哪个 Agent。

    :return: ``mcode`` / ``codex`` / ``claude`` 之一。
    """
    print("")
    print("  要接管哪个 Agent？")
    print("    mcode   —— MiniMax Code（本机默认）")
    print("    codex   —— OpenAI Codex CLI")
    print("    claude  —— Claude Code")
    try:
        raw = input("  直接回车 = mcode：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return "mcode"
    except (OSError, ValueError):
        return "mcode"
    if raw in ("mcode", "codex", "claude"):
        return raw
    return "mcode"


def _ask_task() -> str:
    """问用户要 Agent 干什么。

    :return: 任务描述；用户跳过就是空串。
    """
    print("")
    try:
        raw = input("  要它做什么？（可以直接回车跳过）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("")
        return ""
    except (OSError, ValueError):
        return ""
    return raw


def _acquire_single_instance() -> bool:
    """拿单例锁 —— 防止「双击两次出两个托盘」（v3.8.0）。

    :return: 拿到锁（或者本进程已经拿过）返回 ``True``；
        **别的实例已经在跑**返回 ``False``。
    """
    try:
        from src.gui.single_instance import acquire_single_instance

        return acquire_single_instance() is not None
    except Exception as exc:  # noqa: BLE001 —— 锁不住也不能挡住哨兵
        print(
            "[SENTINEL] 单例锁不可用（{}: {}），本次不做双开拦截。".format(
                type(exc).__name__, exc
            ),
            flush=True,
        )
        return True


def _report_already_running() -> None:
    """告诉用户「哨兵已经在运行中」（v3.8.0）。"""
    try:
        from src.gui.single_instance import report_already_running

        report_already_running()
    except Exception as exc:  # noqa: BLE001 —— 提示失败也要把话说完
        print(
            "[SENTINEL] 哨兵已在运行中 —— 不会启动第二个实例。"
            "（提示渲染失败：{}）".format(type(exc).__name__),
            flush=True,
        )


def _install_process_group() -> None:
    """装上 Job Object —— 进程树同生共死（v3.8.0）。"""
    try:
        from src.gui.single_instance import install_process_group

        group = install_process_group()
        if not group.enabled:
            print(
                "[SENTINEL] 进程树同生共死不可用：{}".format(group.reason),
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001 —— 装不上也不能挡住哨兵
        print(
            "[SENTINEL] 进程树同生共死不可用（{}: {}）。".format(
                type(exc).__name__, exc
            ),
            flush=True,
        )


def _dispatch(argv: Optional[List[str]] = None) -> int:
    """真正干活的入口（被 :func:`main` 包着）。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 进程退出码。
    """
    args = list(sys.argv[1:] if argv is None else argv)

    _prepare_stdout()

    if any(item in ("-h", "--help", "help") for item in args):
        print(HELP_TEXT.format(version=__version__))
        return 0
    if any(item in ("-V", "--version", "version") for item in args):
        return _print_version()

    # ⚠️ v3.8.0：单例拦截放在 ``--help`` / ``--version`` **之后** ——
    #
    #    打包脚本的自检就是「再跑一次 ``sentinel.exe --help``」，
    #    它必须**永远**能跑通，不能被单例锁拦下来。
    #
    #    ``--scan`` 也不上锁：它是一次性的命令行扫描，
    #    不开托盘、不开服务，本来就没有「双开托盘」的风险。
    if "--scan" not in args:
        if not _acquire_single_instance():
            _report_already_running()
            return ALREADY_RUNNING_EXIT_CODE
        _install_process_group()

    _ensure_project_root()
    _ensure_user_config()

    # --- Webhook 真实连通性自检（v2.1.0）---
    # 放在最前面：用户刚启动就该知道「手机能不能收到」。
    _startup_webhook_probe()

    # --- 无参数：先问用户想干什么（v3.0.2）---
    #
    # ⚠️ 为什么不再直接进演示
    # ========================
    #
    # v3.0.1 的实测反馈：「**双击 exe 依然直接进演示模式**」。
    #
    # 演示模式本身没问题 —— 它该做的是「让人先看看长什么样」。
    # 问题是**双击就默认进演示**：用户以为「雷达扫不到真实的进程」，
    # 是因为哨兵**压根没问他要干什么**就替他决定了。
    #
    # 所以这里把选择权交回给用户 —— 三个选项，一句话说清每个是干什么的。
    if not args:
        from src.console import (
            START_MENU_DEFAULT,
            print_start_menu,
            read_start_choice,
        )

        print_start_menu()
        choice = read_start_choice(START_MENU_DEFAULT)
        if choice == "1":
            print("[SENTINEL] 已选择：启动演示模式（体验界面）。")
            args = ["--demo"]
        elif choice == "3":
            print("[SENTINEL] 已选择：接管并启动新的 Agent。")
            return _dispatch_takeover()
        else:
            print("[SENTINEL] 已选择：开启雷达（扫描已有的 Agent 日志）。")
            args = ["--radar"]

    # --- 演示模式（--demo）---
    if "--demo" in args:
        from src.gui.server import main as server_main

        # ⚠️ 演示模式没有启动器接手 —— 体检在这儿跑。
        # ⚠️ 用的是 ``run_startup_health_check`` —— **一个进程只体检一次**，
        # 免得看板那边又打一份，终端上出现两份统计。
        if "--no-health" not in args:
            _startup_health()
        # ⚠️ ``--demo`` 必须**原样传下去** ——
        # 看板自己认这个开关，摘掉它就会报「必须给 --watch」。
        code = int(server_main(list(args)) or 0)
        if code != 0:
            # 看板**没能起来**（端口被占 / 配置有问题）。
            # ⚠️ 这时窗口**绝不能闪退** —— 用户还没看清提示呢。
            print(
                "[SENTINEL] 哨兵没能正常启动（退出码 {}）。".format(code),
                flush=True,
            )
            _wait_for_user()
        return code

    # --- 雷达模式（--radar）---
    if "--radar" in args:
        from src.gui.server import main as server_main

        if "--no-health" not in args:
            _startup_health()
        code = int(server_main(list(args)) or 0)
        if code != 0:
            print(
                "[SENTINEL] 雷达没能正常启动（退出码 {}）。".format(code),
                flush=True,
            )
            _wait_for_user()
        return code

    # --- 命令行扫描一份已有日志 ---
    if "--scan" in args:
        from src.core.engine import main as engine_main

        index = args.index("--scan")
        rest = list(args)
        if index + 1 >= len(rest):
            print("--scan 后面要跟日志文件路径。")
            return 2
        log_path = rest[index + 1]
        del rest[index:index + 2]
        # 引擎的 CLI 用**位置参数**收日志路径。
        rest.insert(0, log_path)
        return int(engine_main(rest) or 0)

    # --- 接管真实 Agent ---
    launcher = _load_launcher()
    return int(launcher.main(args) or 0)


def _startup_health() -> None:
    """启动时跑一次**开箱安全体检**（v2.2.0）。

    用户拿到的是「一个 exe 双击就跑」——
    他**不知道**自己的环境配好了没有。这份体检就是为了回答这个问题：

    * 本地模型（Ollama）开着没？
    * 云端 API 密钥有效吗？
    * Webhook 配了吗？**没配的话半夜跑挂了收不到手机推送！**
    * 工作目录有 ``.gitignore`` 或快照排除规则吗？

    ⚠️ 走的是 :func:`src.core.health.run_startup_health_check` ——
    **一个进程只跑一次**（v2.2.1）。
    否则 exe 入口打一份、看板演示模式再打一份，
    终端上会出现两份统计，用户会以为哨兵出了两次问题。

    失败**绝不**影响启动（架构红线 R5）。
    """
    try:
        from src.config_loader import load_config
        from src.core.health import run_startup_health_check

        run_startup_health_check(load_config(None), workdir=os.getcwd())
    except Exception as exc:  # noqa: BLE001 —— 体检失败不阻断启动
        print(
            "[SENTINEL] 开箱安全体检跳过（{}: {}）。".format(
                type(exc).__name__, exc
            ),
            flush=True,
        )


def _startup_webhook_probe() -> None:
    """启动时做一次 Webhook **真实**连通性自检。

    ⚠️ 配了真实 Webhook 就**真的发一条**出去 ——
    这不是模拟，手机会真的响一声。
    嫌吵可以在配置里写 ``alert.webhook_startup_test: false`` 关掉。

    失败**绝不**影响启动（架构红线 R5）。
    """
    try:
        from src.config_loader import load_config
        from src.core.webhook_probe import startup_probe

        results = startup_probe(load_config(None), __version__)
        for item in results or []:
            print(
                "[SENTINEL] Webhook 自检 → {}：{}".format(
                    item.get("url"), item.get("detail")
                ),
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001 —— 自检失败不阻断启动
        print(
            "[SENTINEL] Webhook 自检跳过（{}: {}）。".format(
                type(exc).__name__, exc
            ),
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
