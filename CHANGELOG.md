# 变更日志

## 未发布 — 命令级策略、边界来源证明、句柄最小化、路径复验、CPU 限速、IO 速率上限、临时区容量卷、环境缺口补救、结构化缺口证据与收尾契约、编程纪律、AppContainer 网络边界、签名接线、受限令牌后端与「跑不了测试」如实跳过、任务级隔离、Claude 式编程工具面、场景模式对齐 Claude 执行与编排语义、子代理默认后台与续聊、后台 shell、任务级工作树会话、Skill 工具（清单 + 按需加载）、ReportFindings 工具、定时工具（Cron + Wakeup）、Workflow 工具（脚本即编排）、NotebookEdit 工具（按单元格编辑）、Monitor 工具（逐行事件流）

- **Monitor 工具：把「命令又输出了一行」变成可投递的系统事件（R 批第 1 项）**：
  按 Claude 原文（`description` / `command` / `timeout_ms` 必填，默认 300000、上限 1800000，
  0 表示不设监控期限；每条 stdout 行一个事件、结束 watch 结束、期间不轮询不 sleep）落成本地
  `monitor`。新增 `services/tools/monitor.py`；`shell_tasks.py`、`tools/__init__.py`、
  `tools/registry.py`、`tools/delegate.py`、`permissions.py`、`permission_rules.py`、
  `agent/graph.py`、`agent/scenarios.py`、`agent/automation.py`、`agent/stall.py`、
  `agent/placeholders.py` 接线。
  1. **逐行事件**：后台句柄表新增 `kind="monitor"`，同一个进程/日志/取消/收尾；输出按字节切行再
     解码（多字节字符被 chunk 切开不会变乱码），stdout/stderr 分得清，空行不投递；命令没结束
     就能取到已产生的事件；结束、超时、取消、`task_stop` 都只以最后一条事件收尾。
  2. **投递有上限**：每次模型调用前注入一次系统通知，文案明确「不是用户发言、不要轮询、不要
     sleep」；一次最多 25 行，积压留在队列下一轮继续；内存队列上限 200 行，超限只丢最旧的行
     并把丢弃条数如实上报。
  3. **句柄与终止**：`mon-` 句柄走同一张表，`task_output` / `task_stop` 与 `shell-` / `wf_` 同一套
     语义；任务收尾取消仍在跑的监控并删日志；被拒绝的调用不留下句柄。
  4. **执行边界不被绕过**：命令仍过 `policy_gate("run_shell")`、安全超时与沙箱档位；shell
     allowlist 同时拦 monitor；子代理直接拒绝；文档/逆向/日常/头脑风暴场景把它当命令工具禁用；
     带 `command` 的工具不获得「总是允许该工具」的档，也不给「总是允许该前缀」（monitor 每次按
     当前档位判定）；手写 allow/deny 规则在 `run_shell` 与 `monitor` 之间互通；高风险动作分类、
     缺命令停滞检测、占位内容审计同样覆盖 monitor。
  测试：新增 `tests/test_monitor_tool_v38.py` 20 例（契约/参数边界/真实进程逐行与 stderr/边跑边投/
     跨 chunk 与多字节/空行/投递上限与积压/队列溢出/通知只注入一次/终止与收尾/四条执行边界）；
     相连 10 个测试文件（monitor/工具授权/后台 shell/命令级策略/场景/NotebookEdit/审批模式/
     停滞/权限规则/占位审计）合计 **199 passed**；变异电池 17/17 全红（逐条改坏实现、实测红集合，
     跑完即按原字节还原），附带一致性改动另做 4/4 红。2026-11-12 后端全量回归
     **2956 passed / 28 skipped / 0 failed**（747.58s，rc=0；28 条 skipped 全是本机环境门：
     docker、受限令牌自检、aiomysql、桌面包产物，与 monitor 无关）。
  偏离：只支持 `command`，Claude 的 WebSocket 变体未移植；`timeout_ms=0` 的「不设期限」仍受
  安全设置的「单条命令超时」约束（默认 120s），回执里如实说明；每轮最多 25 行、队列 200 行；
  `run_shell` 的会话内「总是允许某前缀」不自动扩展到 monitor，同一命令从 monitor 走仍按当前
  档位重新判定。

- **NotebookEdit 工具：按单元格编辑 `.ipynb`（Q 批第 6 项）**：
  按 Claude 原文（`notebook_path` / `cell_id` / `new_source` / `cell_type` / `edit_mode`，
  `additionalProperties=false`，必须先 Read）落成本地 `notebook_edit`。新增
  `services/tools/notebook.py`（单元格渲染 / 磁盘侧编辑 / 工具面）；`tools/filesystem.py`、
  `tools/__init__.py`、`tools/registry.py`、`tools/gateway.py`、`tools/fs_broker.py`、
  `permissions.py`、`agent/graph.py`、`agent/stall.py`、`agent/verifier.py`、`agent/payload.py`、
  `agent/planner.py`、`subagent.py` 接线。
  1. **读**：`read_file` 对 `.ipynb` 默认回单元格视图（`<notebook>` / `<cell id="..." type="...">`，
     代码格附最多 5 条输出、每条截到 400 字符）；带 `offset/limit` 或内容被截断时仍回原始 JSON 行；
     解析不了的 `.ipynb` 回退原始内容，坏文件至少看得见。
  2. **编辑语义**：`replace`（默认）按 id 换源，可顺带改 `cell_type`（代码→markdown 清空
     `outputs`/`execution_count`，反向补默认值）；`insert` 插在目标格之后、省略 `cell_id` 插最前，
     新格自动生成 id；`delete` 删一格。`new_source` 始终必填（`delete` 传空字符串）；
     `replace/delete` 必须有 `cell_id`，`insert` 必须有 `cell_type`；非法模式/类型/缺字段都点名拒绝。
  3. **闸门与写回**：与 `edit_file`/`write_file`/`apply_patch` 共用同一道读后写硬闸门
     （size + mtime + sha256），没读过不许改、读过被改过要重读；写前 `write_backup` + 校验后
     原文件句柄写回；非法 JSON、无 `cells` 数组、单元格不存在（列出可用 id）、无实际变更都拒绝；
     8MB 写上限；写回沿用原缩进（空格/Tab）与 CRLF、`ensure_ascii=false`，不整份重排；
     老 notebook 没有真实 id 时合成 `cell-<序号>` 只用于本次定位、不写回。
  4. **接线**：进写类工具集合（计划模式拦、敏感路径/明文密钥升 high 风险）；gateway 的写目标与
     写根改写认 `notebook_path`（逆向场景自动落分析区）；进 `MUTATING_TOOLS` / 停滞检测 /
     verifier / planner 的 edit 工具组；子代理隔离工具表同口径；系统提示要求 notebook 按格编辑、
     不整份重写；fs-broker 新增 `notebook_edit` 操作并复用同一磁盘函数。
  5. **工具面**：常驻注册、非延迟加载、中文别名「notebook / ipynb / jupyter / 单元格 / 笔记本」
     可检索；风险 medium、超时 20 秒；回执给出模式、单元格 id、变更行数与备份路径，Diff 与
     FileChange 照常落库。
  测试：新增 `tests/test_notebook_edit_v37.py` 30 例（渲染与 id、三种编辑模式、CRLF 与缩进保留、
  读后写闸门、错误形状、工具面与各接线点）；相邻 8 个测试文件合计 **202 passed**；
  变异电池 38/38 全红（每条跑完即按原字节还原并校验 sha）。
  全量回归（最终代码）：**2937 passed, 24 skipped, PYTEST_RC=0（793.93s）**。
  偏离：Read 对 `.ipynb` 默认回单元格视图，而不是 Claude 的「原文 + id 显示行」；
  没有真实 id 的老 notebook 用合成的 `cell-<序号>` 定位、不写回文件；写回保留原缩进与 CRLF
  而不是统一重排；单文件写上限 8MB；没有 Claude 的「id 显示行号」模式。

- **Workflow 工具：脚本即编排（Q 批第 5b 项）**：
  按 Claude 原文（工具契约、meta 纯字面量、agent/parallel/pipeline/phase、scriptPath 优先、
  args 原样、resumeFromRunId 同会话缓存）落成本地 `workflow`。新增
  `services/agent/workflow.py`（脚本解析 / Node 子进程 / JSON 行协议 / 运行与缓存）与
  `services/tools/workflow.py`；`tools/__init__.py`、`tools/registry.py`、`tools/delegate.py`、
  `agent/graph.py`、`agent/runtime.py`、`agent/stall.py` 接线。
  1. **脚本契约**：必须以 `export const meta = {...}` 纯字面量开头（变量/调用/模板插值一律拒绝）；
     `name` ≤100、`description` 单行 ≤240、`phases` ≤50 且 title 唯一 ≤80；
     脚本长度上限 524288 与 NUL 拒绝；正文包进 async 函数，顶层 `await` / `return` 都成立。
  2. **执行语义**：脚本跑在 Node 子进程（`--permission --allow-fs-read=<temp> --disable-proto=throw`），
     只走 stdout/stdin 的 JSON 行协议；`agent()` 转成既有的后台子代理（`spawn_background` + `output`），
     `parallel()` 保序、`pipeline()` 每个 item 内串行而 item 之间并发、`phase()` 必须命中
     `meta.phases`；`args` 以真正的 JSON 值暴露；`schema` 要求子代理只回 JSON 并做
     type/required/properties 骨架校验（`integer` / `number` 不接受布尔）；单条工作流最多派
     50 个子代理、同时在飞 4 个；单个子代理 900 秒、整条 3600 秒上限。
  3. **边界**：脚本不能写文件、不能起进程（Node 权限模型）；`fetch` / `WebSocket` /
     `XMLHttpRequest` / `EventSource` 在预置层替换成报错（网络边界不是内核级，见偏离）。
     工具本身 high 风险、走审批，且只在用户目标里明确出现
     「workflow / 工作流 / 多代理 / fan out / orchestrate / ultracode」之类的请求时允许调用；
     仅「这件事适合并行」不算 opt-in，没开口就先说明规模与代价再问。
  4. **后台与续跑**：启动即返回 `wf_` 句柄；`task_output` / `task_stop` 认该前缀；结束时在
     下一次模型调用前注入一次系统通知（结论由主代理转述）；任务收尾（等审批除外）取消仍在跑的
     运行并摘掉句柄；`resumeFromRunId` 只在本进程内有效，按 `(prompt, options)` 哈希复用缓存，
     旧运行仍在跑时拒绝续跑，传入缓存时先复制、不污染旧运行。
  5. **脚本落盘**：每次运行写到 `.blank/workflows/.runs/<wf_id>.mjs`，并幂等写入
     `.git/info/exclude`（不碰用户的 `.gitignore`）；来源优先级 `scriptPath` > `script` > `name`，
     预定义脚本放在 `.blank/workflows/<name>.mjs`；找不到时列出可用名字。
  6. **工具面与缺口**：延迟加载（`tool_search` 拉起）、非并行安全、中文别名可检索；
     缺 Node 时返回 `skipped` + `data.missing_command=node` + `BLANK_NODE_BIN` 补救说明，
     并接入停滞检测（`workflow` 成功即撤掉结构化缺口，后续命令不再被封）。
  测试：新增 `tests/test_workflow_tool_v36.py` 50 例（脚本契约、结构化结果、来源与落盘、工具面、
  真实 Node 的 parallel/pipeline/phase/args/schema、文件与网络边界、缓存续跑、取消与收尾、
    通知渲染、opt-in 与缺 Node 闸门、任务收尾接线）；变异电池 39/39 全红（每条跑完即还原）。
  全量回归（最终代码）：**2907 passed, 24 skipped, PYTEST_RC=0（901.87s）**。偏离：脚本落在项目 `.blank/workflows/.runs/` 而不是
  Claude 的会话目录；续跑缓存只在进程内（重启即失效）；网络边界靠预置层替换 + Node 权限模型，
  不是内核级网络沙箱；每个 `agent()` 仍是既有的隔离只读子代理工具集，不共享父会话工具面；
  schema 只做骨架校验，不是完整 JSON Schema 实现；工作流随任务收尾取消，不跨任务/跨进程恢复。

- **定时工具：CronCreate / CronDelete / CronList / ScheduleWakeup（Q 批第 5a 项）**：
  按 Claude 原文（5 段本地 cron、7 天期限、抖动、durable、动态唤醒循环）落成本地四个工具。
  新增 `services/agent/cron.py`（纯函数：解析 / 下一次 / 抖动 / 时区）与 `services/tools/cron.py`；
  `tools/__init__.py`、`tools/registry.py`、`permissions.py`、`worker.py`、`app.py` 接线。
  1. **时间语义**：标准 5 段「分 时 日 月 周」，按本地墙钟理解再换算成库里的 UTC naive；
     日与星期同时受限时按 Vixie 语义取 OR；星期 7 映射到 0；解析不了就报错并点名是哪一段，
     不静默降级成「每分钟」。重复任务 7 天后自动结束（期限钉在排定时算出的 `ends_at`，
     不随每次重排往后滚）；一次性任务到点后留 24 小时补跑窗口。
  2. **抖动**：重复任务最多晚「周期的 10%、封顶 15 分钟」，一次性落在 :00/:30 最多早 90 秒；
     偏移由 `sha256(job_id + 到点时刻)` 决定，同一份输入永远得到同一时间（可复现、可测试）。
  3. **存储与执行**：复用 `AgentTrigger`（`event_type=schedule`），不新造表；到点仍走
     `release_due_triggers → release_one → launch_task` 这一条执行路径。第一次到点才建任务
     （prompt 即 goal），后续每一跳沿同一条任务续跑；绑定任务正在跑时把 `fire_at` 顺延 60 秒
     再看，不把这次触发吞掉。重复任务放行后登记下一行，`dedupe_key` 带上下次到点时刻，
     `payload.job_id` 保持稳定——重排后仍能用最初那个 id 取消。
  4. **会话边界**：`durable=false` 只活到本次进程结束，API 启动时统一作废（连同 `ScheduleWakeup`
     的循环），避免重启后凭空补跑；`durable=true` 跨重启保留，到点后 24 小时内补跑一次。
  5. **唤醒**：`delaySeconds` 运行时夹到 [60, 3600]；`stop=true` 结束循环并忽略其他字段；
     同一任务共用一行（`wakeup:<task_id>`），`noop` 连击累加到 `noop_streak` 供界面折叠；
     `reason` 与 `prompt` 必填。回执与事件（`cron.created` / `cron.deleted` / `wakeup.scheduled` /
     `wakeup.stopped`）都给出本地时间，模型与用户看到的是墙上的时刻。
  6. **工具面**：四个工具常驻注册；`cron_list` 只读、并行安全、计划模式放行；中文别名
     「定时 / 提醒 / 日程 / 唤醒 / 轮询」可检索；子代理工具表不含（定时登记会改库）。
  测试：新增 `tests/test_cron_tools_v35.py` 28 例（纯函数 + 工具面 + 工具行为 + worker 接线：
  到点懒建任务、重排、忙时顺延、期限截止、启动清理）；相关回归
  `test_triggers_v162` / `test_schedule_v104` / `test_trigger_controls_v112` /
  `test_claude_tool_surface_v28` / `test_report_findings_v34` / `test_watch_v104`
  合计 **102 passed（35.24s）**；变异电池 26/26 全红（每条跑完即还原）：下一次不严格晚于基准、
  日/星期 OR 变 AND、抖动归零、一次性抖动越界、星期 7 不映射、步长 0 放行、重复过期窗口、
  `ends_at` 缺失、durable 默认反了、列表用行主键、取消不改状态、唤醒不夹取、noop 不累加、
  stop 不取消、reason 不校验、唤醒 fired 行被原样复用、忙时吞掉提醒、cron 不重排、
  到点不建任务、7 天期限失效、启动清理漏唤醒、启动清理误删持久任务、重排丢任务绑定、
  只读/并行/别名三处漏登记。
  全量回归（最终代码）：**2856 passed, 24 skipped, PYTEST_RC=0（829.33s）**。
  偏离：任务编号用稳定 `job_id` 而不是行主键（重排后行 id 每跳都变）；没有 REPL「空闲」这层
  对象，用「绑定任务正在跑就顺延」近似；`durable` 用库里的触发器行承载，而不是
  `.claude/scheduled_tasks.json`；`ScheduleWakeup` 的 `<<autonomous-loop-dynamic>>` 哨兵未实现
  （本地循环由调度器持有，不依赖提示词里的哨兵）。

- **ReportFindings 工具：审查清单一次上报（Q 批第 4 项）**：
  按 Claude 原文（工具描述、zod schema、输出映射）落成本地 `report_findings`。新增
  `services/tools/review.py`；`tools/__init__.py`、`tools/registry.py`、`permissions.py` 接线。
  1. **用法语义**：只在当前生效的代码审查说明点名时使用；一次调用提交整批、按严重程度从高到低、
     最多 32 条；空数组表示没有发现通过核验；回执只说条数，并把「不要再用正文打印同一批发现、
     不要为审查另建文件」写进描述。
  2. **输入契约**：每条必填 `file`（仓库相对路径）/`summary`/`failure_scenario`
     （具体输入或状态 → 错误输出或崩溃）；可选 `line`（从 1 起的整数，bool / 0 / 负数 / 字符串
     一律拒）、`short_summary`（≤60，只写结论）、`category`（≤40，kebab-case）、`verdict`
     （CONFIRMED / PLAUSIBLE，跑过复核才有）、`outcome`（fixed / skipped / no_change_needed，
     只用于修复后重报）；未知字段剥掉、模型给的顺序原样保留；任一条不合法即整批拒绝并点名
     「第 N 条」——收一半丢一半会让报告比实际核验过的少，界面还说不清缺了什么。
  3. **输出与取证**：回执 `已上报 N 条审查发现。` / `没有需要上报的审查发现。`；结构化清单经
     recorder 落 `review.findings`（count / level / findings），同时随工具回执进图与 ToolCall
     产物（沿用打码、gzip、单份 8MB 上限）。非法批次不落事件。
  4. **工具面**：只读、并行安全、计划模式放行、常驻注册（不进 deferred / lean 裁剪）；中文别名
     「代码审查 / 审查发现 / 审查结论 / 报告发现 / 问题清单」可检索；子代理工具表刻意不含它
     ——发现交回主代理，由主代理一次性上报。
  测试：新增 `tests/test_report_findings_v34.py` 36 例（含真实 HTTP 任务：工具表里有
  `report_findings`、事件恰一条、ToolCall 与产物清单一致、合成密钥被打码）；相关回归 218 例
  全绿；变异电池 28/28 全红（每条跑完即还原）：上限判定失效、空数组文案改错、回执条数 +1、
  file / 必填表少项、line 放过 0 与 bool、short_summary / category 上限失效、verdict / outcome /
  level 不校验、未知字段保留、顺序倒置、事件名 / 条数 / 清单走样、level 不进回执、整批校验
  改成跳过坏条目、风险等级提高、schema maxItems / 嵌套 required 缺失、描述允许重复打印、
  默认工具表漏注册、计划模式不放行、并行安全漏登记、中文别名漏登记、子代理白名单误加。
  全量回归（最终代码）：**2828 passed, 24 skipped, PYTEST_RC=0（818.07s）**。
  偏离：工具名用 snake_case `report_findings`（Claude 为 `ReportFindings`）、回执文案为中文；
  清单同时落事件与工具产物，而不只是一条文本回执；子代理不直接持有该工具；`level` 只做透传
  与回执，不改变审查深度本身（审查说明仍由 Skill / 用户指令决定）。

- **Skill 工具：清单进提示、正文按需加载（Q 批第 3 项）**：
  把 Claude 的 Skill 语义从「全部正文每轮注入」改成「先给清单、按名加载」。新增
  `services/tools/skills.py`；`tools/__init__.py`、`tools/base.py`、`tools/registry.py`、
  `permissions.py`、`agent/runtime.py`、`agent/graph.py`、`api/routes/tasks.py`、`subagent.py` 随之接通。
  1. **清单只占一行**：系统提示列出「准确名字 + 一句话说明」，说明取正文首个非空行
     （剥前导 `# > * -`、超长截到 120 字符）。正文不再每轮都塞进提示——启用的 Skill 越多，
     真正相关的那份越不会被淹掉，token 也不再为无关正文付费。
  2. **加载语义**：`skill(skill, args)` 名字必须精确命中清单；前导斜杠直接拒绝并给出修正名
     （`/deploy` → `deploy`）；未知名字 fail-closed 并列出真实清单（明说「不要猜」「CLI 内置
     命令不是 Skill」）；同一任务里重复调用只回一行「已加载」，不重复贴正文。`args` 有
     `$ARGUMENTS` 就替换、没有就附在正文末尾。
  3. **目录与状态分层**：目录是任务级只读快照（`routes/tasks.py` 从 `Skill` 表取 name+instructions，
     同名以后出现的为准），加载状态按会话记；子代理拿目录**副本**，它加载过的 Skill 不会让
     父会话误以为自己也见过正文。
  4. **没有就不摆入口**：一个可加载的 Skill 都没有时，`skill` 从这一轮的工具表里整条移除，
     模型连「有这个工具」都不该知道；子代理同口径。lean 档与计划模式仍保留它（清单里有名字，
     就必须留加载入口）。
  5. **事件**：加载成功落 `skill.invoked`（skill / has_args / chars），走已有 recorder 旁路。
  6. **旧通道保留**：外部集成还在传 `skill_instructions` 的，行为不变（正文仍按工作偏好渲染）。
  测试：新增 `tests/test_skill_tool_v33.py` 30 例（含一条真实 HTTP 任务运行：库里 Skill
  只以「名字 + 一句话」进系统提示、正文不进，工具表里有 `skill`）；变异电池 24/24 全红
  （每条跑完即还原）：
  名字模糊匹配、允许前导斜杠、重复调用再贴正文、args 不透传、摘要不剥 markdown、摘要不截断、
  同名取前者、事件不落、schema 不要求 skill、提示里塞整段正文、运行时无目录仍摆入口、运行时
  有目录也移除、lean/核心集合/中文别名漏登记、计划模式不放行、子代理白名单漏登记、子代理
  不建副本、子代理共享 loaded、子代理无目录仍摆入口、单发与并行两个调用点漏传目录、系统提示
  改用正文当说明、任务快照退回整段正文。全量回归（最终代码）：**2792 passed, 24 skipped,
  PYTEST_RC=0（777.47s）**。首轮曾有 1 条 `test_team_midround_v141` 偶发红（已知团队用例
  抖动，单跑 5/5 全绿），第二轮不再出现。
  偏离：说明取正文首行（库里没有单独的 description 字段）；加载状态只在本任务进程内，
  重开任务会重新加载；没有实现 Claude 的「Skill 目录扫描 + 权限白名单」，仍以库表为准。

- **任务级工作树会话：`enter_worktree` / `exit_worktree`（Q 批第 2 项）**：
  把 Claude 的 EnterWorktree / ExitWorktree 落成「这个任务此刻在哪棵树里干活」的一条会话事实。
  新增 `services/worktree_session.py` 与 `tools/worktree.py`；`tools/base.py`、`tools/__init__.py`、
  `tools/registry.py`、`agent/runtime.py`、`agent/graph.py`、`services/workspace.py`、
  `services/worktree.py` 随之接通。
  1. **换根只换一处**：`ToolContext` 新增 `recorder` 字段；enter 成功后用
     `object.__setattr__(context, "root", ...)` 切根并重置 `FileStateCache`——shell 的 cwd、文件工具、
     git、安全策略的路径基准读的都是同一个 `context.root`，不存在「一半在旧树里」。
  2. **创建语义**：`name` 在 `<repo>/.blank-worktrees/<name>` 新建 `blank/worktree-<name>` 分支
     （名字只允许字母/数字/点/下划线/短横线、最多 64 字符）；`path` 只能进入 `git worktree list`
     里已注册、且属于当前仓库或工作区内嵌套仓库的树；`name` 与 `path` 互斥，已在会话里不能再
     `name` 新建。
  3. **退出闸门**：`keep` 只摘记录；`remove` 仅对本任务用 `name` 创建的树生效，`path` 进入的只能
     `keep`；有未提交改动（含未跟踪文件）或未合并提交时拒绝并逐个点名，确认丢弃必须显式传
     `discard_changes=true`；`remove` 风险等级为 high，走审批。`exit_worktree` 登记进
     `ARGUMENT_SCOPED_TOOLS`：批准一次 `remove` 不能记成「整个工具以后都免问」；`enter_worktree`
     的风险与参数无关，风险固定为 medium，不挂 `risk_for`。
  4. **卫生**：建树后把 `/.blank-worktrees/` 写进仓库本地 `.git/info/exclude`（不动用户的
     `.gitignore`），搜索/快照（`TREE_IGNORE`）与子代理整树复制（`COPY_IGNORE`）都跳过它。
  5. **会话生命周期**：按 `task_id` 存在进程内；`runtime._prepare` 先查 `active_root`，审批恢复后
     仍在同一棵树（目录被外部删掉则退回共享工作区）；`_finish` 只在非 `waiting_approval` 档摘记录、
     不删树——没合并的工作留给用户。
  6. **事件与出参**：`worktree.entered` / `worktree.exited` 落事件流（含根、原根、仓库、分支、
     owned、action/removed/branch_deleted）；回给模型的 data 只带仓库内相对路径，不泄漏主机绝对路径。
  7. **提示词**：系统提示写死「只在用户或 AGENTS.md 明确要求时开；keep/remove 语义；remove 遇
     未提交改动会拒绝；不要为了删而删，也不要自己编 discard_changes」。
  测试：新增 `tests/test_worktree_tools_v32.py` 34 例；相关回归（含本文件）
  `test_worktree_hooks_v118`、`test_worktree_parallel_v13`、`test_background_shell_v31`、
  `test_tool_wiring_v150`、`test_tool_search_v13`、`test_tiered_tools` 合计
  **120 passed（30.86s）**。变异电池 28/28 全红（每条跑完即还原）：去掉 owned/脏树/提交闸门、
  keep/remove 不摘记录、不删分支、目录树标成非本任务、不写 exclude、目录改名、名字校验失效、
  会话内重复 enter、链接树拿自己当主仓库、游离 HEAD 报成 HEAD、出参泄漏绝对路径、active_root
  不识别缺失目录、enter/exit 不换根、换根不重置读状态、discard 默认放行、remove 不按高风险、
  事件不落、工具不按需登记、提示词漏纪律、恢复退回共享根、收尾不摘会话、遍历不跳过工作树、
  复制不跳过工作树、工具档漏登记 exit_worktree。全量回归（最终代码）：**2762 passed,
  24 skipped, PYTEST_RC=0（1117.22s）**。首轮曾有两条 `test_team_midround_v141` 偶发红
  （已知团队用例抖动，与单跑/子集不符），第二轮不再出现；另修掉首轮暴露的真回归
  （`enter_worktree` 挂 `risk_for` 未登记工具档）后重跑。
  偏离：工作树建在 `.blank-worktrees/` 而不是 Claude 的 `.claude/worktrees/`；没有暴露
  `baseRef`（一律从 HEAD 创建）；`path` 只认已注册的 git 工作树，不接受任意目录。

- **后台 shell：`run_in_background` + 输出/终止配对（Q 批第 1 项）**：
  把 Claude 的 Bash 后台语义落到执行图上：命令先跑起来、拿句柄继续干活、结束由系统通知，
  不轮询、不 sleep。新增 `services/shell_tasks.py`，`tools/shell.py`、`tools/delegate.py`、
  `agent/graph.py`、`agent/runtime.py`、`subagent.py` 随之接通。
  1. **非阻塞**：`run_shell(run_in_background=true)` 立即返回 `shell-<8hex>` 与 `status=running`，
     后台协程真正起进程；`description` 进通知便于辨认。未显式给超时按 600 秒上限跑（前台仍是 30 秒：
     长构建用 30 秒会被直接判死）。
  2. **流式落盘**：宿主执行路径边跑边写日志（系统临时目录，不进工作区）；单条命令 2MB 上限、
     超限写截断标记，读回只取尾部 64KB。沙箱路径不流式，结束时整段补写，不会留下空日志。
  3. **完成通知只投递一次**：`shell_tasks.drain_notifications` 与子代理通知同一注入点——
     `call_model` 在压缩后的消息尾部补一条 `system` 通知（含句柄、状态、退出码、输出末尾），
     并落 `shell.notification` 事件；已通知的句柄仍留在表里，`task_output` 照样取完整输出。
  4. **统一句柄路由**：`task_output` / `task_stop` 接受通用 `task_id`，旧名 `agent_id` / `shell_id`
     继续接受；`shell-` 前缀或显式 `shell_id` 路由到后台命令分支，其余走子代理。schema 去掉
     `required: ["agent_id"]`，旧名调用不会被 schema 直接拒掉。
  5. **终止按进程树**：Windows 下命令走 cmd 外壳，只 terminate 外壳会把 python 等孙进程留下继续
     写文件——`taskkill /PID <pid> /T /F` 连树杀，超时与取消两条路径共用；`task_stop` 等待协程
     结束再回报，不谎报「已结束」。
  6. **收尾不留孤儿**：任务走到终点（成功/失败/取消）时 `runtime._finish` 取消仍在跑的后台命令、
     删除日志；等待审批不是终点，那一档保留句柄。子代理上下文 `allow_background_shell=False`
     并在真实装配线判据里钉住：子代理的工作副本本轮结束即清理，后台进程活不到取结果。
  7. **提示词与工具描述**：系统提示写死「长构建/起服务传 `run_in_background=true`，结束时收到
     通知，不必轮询、也不要为此 sleep；进度 `task_output`、终止 `task_stop`」；`run_shell`
     描述同步。
  测试：新增 `tests/test_background_shell_v31.py` 20 例（15.05s）；相关回归集
  `test_claude_tool_surface_v28`、`test_background_subagent_v116`、`test_claude_subagent_default_v29`、
  `test_claude_loop_parity_v27`、`test_shell_dialect`、`test_shell_decode_v149`、
  `test_shell_quoting_v53`、`test_shell_relative_exec_v161`、`test_host_shell_argv_v145`、
  `test_sandbox_matrix_v21`、`test_sandbox_bypass_v115` 合计 **146 passed（31.48s）**。
  变异电池 21/21 全红（每条跑完即还原）：忽略 `run_in_background`、后台超时退回 30 秒、
  不接流式回调、通知重复投递、只改状态不取消、收尾不点名、收尾不删日志、子代理放行、
  提示词漏规则、不路由 shell 句柄、旧名优先不认 `task_id`、前缀不识别、通知不注入、
  超时报失败、非零退出报完成、通知不带输出末尾、收尾不收、审批也收、取消只杀外壳、
  读到不落盘、schema 缺 `task_id`。其中「收尾后日志不删」第一轮是空判据（无输出的命令
  根本不建日志），补上「先等日志落盘再收尾」后才按预期变红。
  全量回归（最终代码）：**2725 passed, 24 skipped, PYTEST_RC=0（802.21s）**。首轮
  2724 passed / 1 条团队偶发红（先后红了 `test_team_midround_v141` 里两个不同用例，
  单文件连跑两次 5/5 全绿，二轮全量 rc=0）；未调大任何超时，按已知偶发登记。
  偏离：沙箱路径不流式（结束时整段写）；完整输出走 `task_output` 而不是 Claude 的
  「Read 输出文件」（临时日志在系统临时目录，Read 只读工作区相对路径）。

- **Glob / Grep 参数面 / ListAgents 对齐 Claude Code 工具表，子代理默认后台与续聊（P 批）**：
  把 Claude 的 Glob 与带丰富参数的 Grep 拆成可失败判据，broker 边界与宿主同源；
  Agent 工具的「默认后台 + SendMessage 续聊」语义也一并落到执行图与工具面上。
  1. **新增 `glob`**（`tools/filesystem.py`）：`*` / `?` / `[]` / `{a,b}`，`**/` 能匹配零层目录
     （`**/*.md` 命中根部 README.md）；不带斜杠按文件名、带斜杠按相对路径；忽略目录与
     扫描预算跟搜索同一套；按 mtime 倒序，limit 1–1000（默认 200），截断标 `truncated`。
  2. **`search_code` 扩成 Grep 参数面**：`output_mode=content|files_with_matches|count`、
     `glob`、`type`（未知类型直接报错并列可用项）、`-i`、`multiline`、`-A/-B/-C`、`-o`、
     `-n`、`head_limit`、`offset`。上下文行以 `path-line-` 渲染但不计入命中数；
     `offset` 先于 `head_limit` 生效。
  3. **broker 同源转发**（`tools/fs_broker.py`）：新增 `glob` op；`search` 把全部新参数带进
     子进程边界（含 `case_insensitive` / `multiline` 的正则编译）。宿主能用的语义，
     任务级隔离与 fs-broker 通道一样能用。
  4. **新增 `list_agents`**（`tools/delegate.py` + `subagent.py`）：列出本任务后台子代理的
     agent_id / agent_path / goal / status / summary，running 与 completed 分得清；
     只读，进并行安全表，不替代 `task_output` 取完整结论。
  5. **只读视图同步登记**：CORE / LEAN / PARALLEL_SAFE / 计划模式白名单 / 子代理
     READ_ONLY_TOOLS / 输出预算 / planner 提示全部包含 `glob`，漏登记会让工具消失或被
     计划模式误拒。
  6. **提示词补两条 Claude 规则**：难以撤销或对外的动作先确认，一次批准只覆盖那一次；
     删除、覆盖、移动文件前先看目标。专职工具清单加入 `glob`。
  7. **子代理默认后台**（`tools/delegate.py`）：不传 `run_in_background` 即在后台跑，
     立即返回 agent_id；只有下一步马上要结论时才传 `false`（旧名 `background` 继续接受）。
     系统提示同步写死这条纪律，`DELEGATE_TOOLS` 记账不因默认值变化而漏掉派出部分。
  8. **收尾有界等待**（`subagent.settle_background` + `agent/graph.py`）：交付前给后台
     子代理 90 秒窗口。窗口内结束的结论写进交付提示词并落 `subagent.settled` 事件；
     仍在跑的显式取消、在交付正文里点名（模型漏写也不影响用户看到事实）；已交付的
     结论不再作为「后台已结束」通知重复投递。没有后台子代理时交付正文不变。
  9. **新增 `send_message` 续聊**（`subagent.send_message` + `tools/delegate.py`）：按任务
     保存子代理对话（上限 20 会话 / 60 消息 / 单条 8k 字符，系统提示永远保留），续聊复用
     原 agent_id 与 agent_path 并带上 history。隔离子代理副本结束即清理 → 明确拒绝；
     未知句柄如实说没有上下文；仍在后台跑的句柄提示先 `task_output`；空消息拒绝。
     工具进 `DEFERRED_TOOLS` 与别名表（续聊/追问/继续问/接着查），用量计入委派账。
     系统提示补一条「追问用 send_message，重新 spawn 是全新上下文」。
  测试：新增 `tests/test_claude_tool_surface_v28.py` 21 例（9.61s）、
  `tests/test_claude_subagent_default_v29.py` 6 例（2.21s）、
  `tests/test_claude_send_message_v30.py` 9 例（2.14s）；相关回归集
  `test_claude_loop_parity_v27`、`test_subagent_v12`、`test_background_subagent_v116`、
  `test_tiered_tools`、`test_tool_search_v13`、`test_parallel_budget_v26`、
  `test_worktree_parallel_v13`、`test_memory_types_v114`、`test_agent_v04` 合计
  **143 passed（51.92s，含三个新判据文件）**；P1 另外的 broker/权限回归集见上条 136 passed。
  变异电池：P1 8/8、P4 8/8、P5 10/10 全红（每条跑完即还原）。P1 见上条；P4 覆默认同步、
  别名不识别、收尾不取消、不标已通知、交付不等待、提示词不提取消、正文不点名、不发
  `subagent.settled`；P5 覆续聊不传历史、不保存对话、不区分运行中句柄、隔离也放行、
  工具未注册、重复注入系统提示、用量不入账、工具变常驻、提示词不教续聊、收尾不摘缓存。
  全量回归（最终代码）：**2705 passed, 24 skipped, PYTEST_RC=0（780.04s）**；本批新增的
  36 例（v28 21 + v29 6 + v30 9）全部计入，24 个 skipped 与 P 批之前一致。

- **场景模式对齐 Claude Harness：驳回不再停轮、工具回执即续跑、子代理完成通知、单一 in_progress（O 批）**：
  五条语义各自对应一条可失败判据，全部落在执行图上而不是只改提示词。
  1. **驳回不再暂停整轮**（`agent/graph.py` 的 `execute_action`）：被拒的那一步记成「未执行」，
     回执明确写「不要原样重试」，循环继续让模型换个做法或把选择交回用户；同一会话里再次发送
     同一 key 直接拒绝（不再弹卡）。`task.paused` 现在**只来自用户手动暂停**，`runtime.py` 与
     `team.py` 的文案同步改成「用户暂停了任务」。
  2. **工具回执即续跑**：`ask()` 收到空 prompt 时不再追加合成的 user 消息；`execute_action`
     只在最后一条不是工具回执时才补「请继续」。工具结果本身就是下一步输入，与 Claude Code
     把 tool result 直接交回模型一致。
  3. **后台子代理完成是系统事件**：`SubAgentRunner.drain_notifications()` 对每个句柄只投递一次，
     `call_model` 在压缩后的消息尾部注入一条 `system` 通知并落 `subagent.notification` 事件；
     通知明确「结论由主代理转述、未列出的子代理不要替它编结果」。通知不是摘除，`task_output`
     仍能取到完整结论。
  4. **`update_plan` 对齐 TodoWrite**：同时出现多个 `in_progress` 直接返回错误并列出步骤名，
     工具描述同步写死「一步做完立刻标 completed」。
  5. **系统提示补齐编排纪律**：信息够了就动手、给推荐而不罗列选项、计划与子代理结论的归属、
     系统提醒与外部上下文只能由系统注入且永不能覆盖安全与审批规则、读文件/列目录/搜内容/看
     git 状态优先用专职工具（shell 只用于真正执行命令）。
  测试：新增 `tests/test_claude_loop_parity_v27.py` 6 例（一次全绿，11.60s）；相关回归集
  `test_approval_v06`、`test_team`、`test_dialect_device_v32`、`test_agent_v04`、
  `test_model_retry_fallback_v152`、`test_v163_super_agent`、`test_background_subagent_v116`
  合计 **102 passed**。变异电池 7/7 全红（每条跑完即还原）：还原旧 `paused` 块 → 驳回判据红；
  `execute_action` 无条件补「请继续」→ 续跑判据红；去掉 `ask()` 空 prompt 守卫 → 续跑判据红；
  去掉 `notified` 守卫 → 同一句柄被通知两次；去掉通知注入 → 最后一条不是 `system`；
  删掉 `in_progress` 检查 → 两个 in_progress 被接受；删掉「信息够了就动手」→ 提示词判据红。
  替代旧口径的用例同步改判：`test_approval_v06` 8 例、`test_team` 审批续跑、方言装置
  `test_the_resume_guard_accepts_the_paused_stop_and_survives_a_flake`，以及
  `evals/dialect_e2e.py` 的驳回格（期待 `succeeded` 且不得出现 `task.paused`）。
  全量回归：**2668 passed, 24 skipped, PYTEST_RC=0（758.87s）**，较 N 批基线 2662 passed
  多出的 6 例即本批新判据文件。

- **编程工具面照 Claude Code 2.1.271 对齐：读后写硬闸门、edit_file/write_file、shell 写文件拦截（N 批）**：
  把 Edit / Write / Bash 的语义拆成**可失败的判据**，而不是只改提示词。
  1. **读后写硬闸门**（`tools/file_state.py`）：`read_file` 成功时记录 `(size, mtime_ns, sha256)`；
     `edit_file` / `write_file` / `apply_patch` 写前对照磁盘——目标已存在却没读过 → `not_read`，
     读过之后被改过 → `stale`（stat 一致时再用 sha256 闭合预检到写入之间的替换）。
     写成功后按磁盘现状重记，连续编辑不必重读；读取被截断（>512KB）时哈希留空，只用 stat 比较。
  2. **新增 `edit_file`**（`tools/editing.py`）：精确串替换。`old_string` 必须与文件完全一致且唯一；
     多次出现却未设 `replace_all` → 失败并回报出现行号；0 次 → 失败并带回最接近的上下文；
     `old == new` → no-op 失败；只能改已有文件（新建走 `write_file`）；CRLF 归一到 LF 匹配、
     按原换行风格写回；写前自动备份并落 `FileChange`。
  3. **新增 `write_file`**：整文件写入。新建免读；覆盖已有文件必须先读，读过之后被改过要重读；
     内容相同返回 `unchanged`（不写备份、不落变更记录）。
  4. **apply_patch 过同一道闸门**：目标已存在却没读过、或读过之后被改过，直接拒写。守卫随请求
     进 fs-broker 与 AppContainer 一次性 broker，判定在哪个执行边界都成立。
  5. **shell 硬拦截**（`safety.py`）：`--no-verify`、`--no-gpg-sign`、`commit.gpgsign=false`、
     `git rebase -i`、`git add -i` 进内置拒绝（引号内的字样不算动作）；重定向、
     `Set-Content`/`Add-Content`/`Out-File`/`Clear-Content`/`Tee-Object`、`| tee` 一律拒绝——
     文件操作走专用工具，备份、diff 与审计才看得到。`2>&1`、`2>nul`、`> /dev/null`
     与引号内的 `>` 不误杀。
  6. **提示词与工具描述对齐**：系统提示第 3/6 条改为 `edit_file` / `write_file` / `apply_patch`
     的分工与「编辑成功后不要重读验证」；新增「互不依赖的只读调用可同批并行」与
     代码引用写 `路径:行号`；`read_file` 描述补「编辑前必须读、编辑后不要重读」；
     `parallel_read_tools` 从 beta/默认关提升为 **stable/默认开**（关掉即完全回到串行旧行为）。
  测试：新增 `tests/test_claude_parity_v24.py` 60 例；关联并行 / 开关 / 补丁三套件合计 91 例全绿。
  变异 8/8 全红：撤掉闸门红 8 例、撤 `old_string` 唯一性红 1、撤 stale 判定红 5、
  撤 shell 写文件拦截红 7、并行退回默认关红 1、`read_file` 不记状态红 3、
  `edit_file` 写后不重记红 1、`write_file` 写后不重记红 1（每条变异跑完即还原源码）。
  收口回归跑了两轮：首轮 `2660 passed, 24 skipped, 2 failed`——两条都是
  `test_sandbox_bypass_v115` 拿 `echo > 文件` 当副作用探针，撞上 N4 新增的 shell 写文件硬拦，
  越界闸门根本没被走到；探针换成 `mkdir`（safety 明确不拦）后
  **`2662 passed, 24 skipped, 0 failed`（790.98s，pytest_rc=0）**。
  更早一批 13 条旧红先行修掉：`safety.py` 两处真漏判（引号包住的越界路径、引号包住的重定向
  目标都按抹引号后的命令判定）与计划模式归因被 `shell_file_write` 抢先；其余是闸门引入后的
  陈旧夹具（9 处补「先读后写」回合、`crash_resume` 改用 `write_file`、提示词断言同步新文本）。
  新增的两条 safety 判据做了变异：引号重定向退回抹引号扫描红 2 例、引号路径退回抹引号扫描
  红 1 例（均已还原源码后复跑 60 例全绿）。
  偏离项（本轮不移植）：后台 shell（`run_in_background`）、NotebookEdit、PDF/图片 Read；
  路径仍要求**工作区相对路径**（Claude 用绝对路径）；未实现「`old_string` 是上一次
  `new_string` 子串」的链式编辑拒绝。抓包原文不入库，只留在 `E:/tmp/rea6`、`E:/tmp/rea7`。

- **任务级隔离：每个任务一个独立可写根 + 一个只属于它的 AppContainer 包（J3）**：此前隔离靠
  路径闸门与审批——同一个 OS 用户下的另一个任务拿到绝对路径就能 `open()`。现在打开
  `BLANK_TASK_ISOLATION=1`（管理员 + AppContainer）后，任务在副本里干活：
  - **副本不是 `git worktree`**：worktree 的 `git add` 会写共享对象库，等于给任务一把改整个
    仓库历史的钥匙（实测：只授权任务根时 git 直接「拒绝访问。」，授权共享对象库 M 后才成功）。
    改用 `git clone --shared`：副本有自己的 `.git`，对象库只读借用；源仓库的 `.git/objects`
    只以 RX 授给本任务包，克隆里的 `git add/commit` 不碰主仓库。
  - **基线口径**：克隆 HEAD → `overlay_working_tree` 叠上未提交改动 → `commit_baseline` →
    收紧 ACL。任务看到的仍是真实工作树，`git diff` 只算任务自己的改动；收尾复用
    `worktree.apply_changes` 的保守合并（并发改过的文件只交回 diff，删除类改动不自动执行）。
  - **包与根同生共死**：包名按任务键派生（`BlankTask-<sha256[:12]>`，稳定且唯一）；任务根
    `_set_private` 去掉继承 ACE 后再 `_grant M` 给本包。子代理在隔离时也建自己的克隆与包，
    正常结束/取消/fan_out 三处收尸都把副本与包一起删；结果字段不泄漏包名。
  - **文件工具换身份执行**：AC 进程没有 stdin 管道，长期对话的 stdio broker 用不了。新增一次性
    broker（`--once REQUEST RESPONSE`）：请求/响应落在宿主建、只授本任务包的 IPC 目录，
    响应文件是唯一结果来源；任何一层失败直接失败，**不回退宿主**。命令走生产
    `execute_command(isolation_profile=...)`，档位无沙箱、`host_bypass`、后端不是 AppContainer
    三条都整条拒绝（`isolation_blocked`，不起进程）。
  - **建根前先验后端**：生产探针第一次跑就撞上「任务根建好了，命令却被拒，因为 AC 自检还没
    完成、后端仍是 local」。`create()` 现在同步跑 AC 行为自检（在线程里，不冻事件循环），
    并确认此刻选中的后端就是 AppContainer——自检不绿或会被 Docker/受限令牌抢走时整任务拒绝，
    不建「没人能进的副本」。
  - **清理不许假报干净**：`rmtree(ignore_errors=True)` 在 Windows 上被 `.git` 只读对象挡住，
    会「只删一半」并静默返回成功（生产探针实测：release 报无错误，副本目录还在
    `%TEMP%/blank-worktrees`）。现在删除时逐项清只读位重试；`release` 再检查目录是否仍在，
    仍在就写进 `errors`，不报「已释放」。
  - **任务根容量卷**：`BLANK_TASK_DISK_MB>0` 时任务根落在固定容量 VHDX 卷上（复用 J2 的
    `windows_volume`），写满由内核返回 ENOSPC，释放时卸卷并删除挂载点。
  测试：新增 `tests/test_task_isolation_v23.py` 28 例（包名稳定/唯一、源对象库授权、建根
  三条拒绝、once 协议四种形状、filesystem/patch 隔离路由、shell 三条 fail-closed、子代理
  收尸、收尾事件序、只读目录删除、残留目录记账）。变异 7/7 全红：A 路由去掉隔离 → 路由
  用例红；B shell 去掉档位拒绝 → 拒绝用例红；C `secure_tree` 去掉源对象库授权 → 该用例红；
  D 子代理正常结束不删包 → 收尸用例红；E 失败也 harvest → 收尾用例红；F 撤掉只读清理 →
  只读目录用例红；G 撤掉目录残留检查 → 残留记账用例红。生产路径探针（2026-11-11，管理员，
  `E:/tmp/j3-prod/probe_j3_production.py`）17/17：生产 `execute_command` 在 AC 内写成功；
  `_list_files/_read_file` 的 `executed_by=fs_broker_appcontainer`；另一任务包用绝对路径与
  `..\` 相对路径读都被 `PermissionError` 拒，宿主对照读得到；harvest 合并回主工作区；A/B
  释放后目录、卷、包均无残留；64MB 任务卷写到 53,477,376 字节后 `OSError errno=28`。
  后端全量 2602 passed / 24 skipped（888.11s，rc=0）。未验：另一 OS 用户不可达、
  junction/符号链接绕过、多任务并发卷生命周期（见「未验证」第 3、4 项剩余格）。

- **编程纪律：照 Codex / Claude 原文补三处（M 批）**：把两家 0.154.0 / 2.1.271 的
  编程规则逐条比对（抓包原文不入库，只留在 `E:/tmp/rea6`），挑出三处我们代码里真缺的：
  1. **破坏性 git 进不可逆闸门**：`permissions.IRREVERSIBLE` 原来只拦 `git push --force`，
     连 `reset --hard`、`checkout -- .`、`clean -fdx` 都算可逆——它们丢掉的是未提交改动，
     工作区快照赎不回来，却会被 guardian 代批、还会被记成「总是允许」。现在拦
     `reset --hard`、`checkout --/./-f`、`restore`（`--staged` 除外）、带 `-f` 的 `clean`
     （`-n` dry-run 放行）、`branch -D`/`--delete --force`、`push --force/-f`，都支持
     `git -C <repo>` 前缀。不可逆 → 任何模式、任何前缀规则都不放行，也不进免问记忆。
     判据取自 Claude「NEVER run destructive git commands (push --force, reset --hard,
     checkout ., restore ., clean -f, branch -D) unless the user explicitly requests these
     actions」与 Codex「Never use destructive commands like `git reset --hard` or
     `git checkout --` unless the user has clearly asked」。
  2. **git 工具补只读 `blame`**：Codex 原文「Use `git log` and `git blame` to search the
     history of the codebase if additional context is required.」——我们只开了
     status / diff / log，「这一行为什么是这样」查不了，模型只能猜。`blame` 必须带
     `path`，并用 `--` 分隔路径防参数注入；仍属只读子命令，不改任何写边界。
  3. **系统提示词新增「编程纪律（要动代码时）」11 条**：改根因不打表面补丁、无关 bug/坏测试
     不修只在交付里说一句（Codex「Do not attempt to fix unrelated bugs or broken tests」）；
     跟随现有风格、不引入没被要求的抽象/依赖/格式化器（Claude「Write code that reads like
     the surrounding code」）；注释只写为什么、不加版权头；优先改现有文件、不主动建
     README/文档（Claude「NEVER create documentation files unless explicitly requested」）；
     写文件只走 apply_patch，不用 shell 重定向（审计与回滚看不到）；验证从最小范围开始，
     项目没有测试就不硬加框架；静态/类型检查报错查到底（Claude「investigate errors — don't
     dismiss as 'unrelated'」）；能跑就真跑一遍看行为，跑不了说清哪部分没验证；不提交、
     不建分支、不推送、不 force push 主分支、不跳钩子、不改 git 配置；工作区已有改动属于
     用户（Codex「Existing or new changes belong to the user」）；查不到的别猜，先查
     git log/blame。
  测试：新增 `tests/test_coding_discipline_v26.py` 47 例（17 条破坏性命令全命中、19 条
  安全命令零误伤、bypass 档仍 ASK、前缀不记、blame 参数与真实 `git blame` 集成、提示词
  六条契约），关联七套件 176 例全绿（21.57s）。变异 4/4 全红：去掉 `reset --hard` 红 5 例、
  `checkout` 一律拦红 2 例（误伤反例）、去掉 `blame` 子命令红 1 例、删掉编程纪律段落红
  6 例。后端全量 2572 passed / 24 skipped（779.00s，rc=0）。真模型点验（2026-11-11，隔离实例
  `:8061` + 客户端 DeepSeek Key）：blame 轮 `29a3db21…` succeeded——模型先 `tool_search` 找到 git
  工具，再调 `{"subcommand":"blame","path":"app.py"}`，`Probe Author` 出现在回执里；reset 轮在模型
  发出 `git reset --hard` 时弹出审批卡（`permission_rule=irreversible`），探针驳回后任务 `paused`，
  命令未执行。

- **缺口证据与收尾契约：照 Codex / Claude 的原文补 K 批两处盲区（L 批）**：把 Codex
  0.154.0 与 Claude Code 2.1.271 的真实系统提示词拆了一遍（抓包原文不入库），按它们
  对「缺东西之后怎么办」和「什么时候才能收尾」的写法，找出 K 批闸门的两处真漏判：
  1. **run_tests 的缺工具链回执根本不进缺口账本**：`本机没有 go 工具链，测试没有执行`
     既不匹配任何一条失败正则、也不是 `KIND_ENV_MISSING`，K 批只有文字判据，于是
     「需要你装 Go」这类交付永远推不回来。现在 `testing.py` 在
     `data.missing_test_runner` 里写明名字，`stall.structured_missing_tool` 只认这份
     **结构化**字段（不认 message 里的模型自述——让被审的一方自己交证据，闸门就失效）；
     `run_tests` 跑通后把这批证据整批撤回，否则真的跑绿了闸门还会触发。
  2. **项目依赖安装不算「补齐尝试」**：Codex 原文「Install dependencies into that
     environment with its package manager. In uv-managed environments, `uv pip install ...`
     remains the preferred path.」；K 批只认 winget/choco/scoop/msi/下载，模型
     `pip install pytest` 之后如实交付反而被推回一轮。`remediation._OBTAIN` 补
     pip/uv/poetry/npm/pnpm/yarn/go/dotnet/cargo/gem/composer，`hint()` 第 2 步补项目
     包管理器路径。
  3. **回执要带出口**：缺工具链的 skipped 回执不再只有一句「本机没有 X」，补上
     找现成 → 包管理器 → 绝对路径/PATH → 真装不上再交代的顺序（对应 Codex「tell the
     user which dependency is missing and how to install it into their active
     environment」）。
  4. **收尾契约进系统提示词**：三条——问题没解决、也没说清卡在哪就不收尾（Codex
     「keep going until the query is completely resolved… Only terminate your turn when
     you are sure that the problem is solved」）；登录/密钥/拍板这类只有用户能给的信息
     问一句就是正确的下一步；结果如实，测试没过贴输出、跳过的步骤说跳过（Claude
     「Report outcomes faithfully: if tests fail, say so with the output; if a step was
     skipped, say that」）。规则 7 补拒绝语义（Claude「a denied call means the user
     declined it — adjust, don't retry verbatim」）。
  测试：新增 `tests/test_agent_continuation_v25.py` 13 例（结构化证据入账与撤账、九种
  包管理器算尝试而确认不算、go 缺工具链→交回→推回整链、测试成功不推、回执带出口、
  提示词顺序与措辞），关联九套件 152 例全绿（37.42s）。变异 4/4 全红：结构化缺口不入账
  红 2 例、跑通不撤账红 1 例、项目包管理器不算尝试红 2 例、删掉收尾条件红 2 例。后端
  全量 2525 passed / 24 skipped（760.22s，rc=0）。真模型点验（2026-11-11）：`run_tests` 的
  `skipped` 回执把「本机没有 npm 工具链 + 补齐顺序」原样带到模型面前（task `d59fe7a1…`），模型据此
  去安装而不是交回；交付按「结论 / 下一步」两段写，跳过的步骤与未验证的部分都如实列出，验证闸门
  按回执把它们记成 `no_false_claim` / `validation.status` 的硬事实。推回闸门本身本轮没有触发
  （模型在第一次交付前就自己补了），这一步仍只有单测与装置证据。

- **环境缺口不再停在「做不了」（K 批）**：真实读数（安装版 `blank.db`，任务 `4378855e…`，43 步）
  里，agent 确认本机没有 `node`/`npm` 后最终交付只剩「需要你：装上 Node 后重跑 `npm test`」；
  用户催办原话「为什么你看到没有后你不想办法自己装呢」「你怎么不自己装呢」被判成 answer/advise，
  而 advise 会打开只读开关，`run_shell` 连调都调不了。四处改动：
  1. **意图**：`intent.demands_action` 识别两种语序的催办（「你为什么/怎么不……」与
     「为什么/怎么你……」），在 `guess_intent` 里早于 `_OPINION`/advise 判定；`create_plan`
     再加一道确定性覆盖——模型把催办判成 answer/advise 时改判 `act` 并记 `intent.overridden`
     事件。提示词也补了「这类「为什么」不是在要解释，是在说「你去做」」。
  2. **空转断路器**：`where /R C:\ node.exe`、`if exist …`、`dir /s /b` 这类**找程序**不再被当成
     「又用了一次程序」；`cmd /c node -v`、`powershell -NoProfile -Command node -v`、
     `pnpm exec node -v` 仍按使用拦（包装词按原样与 basename 双比，修掉 `/c` 被剥成 `c` 的漏拦）。
  3. **环境缺口闸门**（`services/agent/remediation.py` + `execute_action`）：本任务有**真实的**
     `env_missing` 回执、答复是在把活交回用户（需要你装/装上后/装不了/做不了）、且整个任务没有
     一次安装/下载尝试——三条同时成立就把任务推回执行，并在系统侧注入补齐顺序（现场确认 →
     包管理器/portable/磁盘上找现成的 → 绝对路径或加 PATH 重跑），上限 2 轮；readonly、预算耗尽、
     已经试过安装时豁免。推回只进下一次模型调用，不改用户原话与历史。
  4. **系统提示词**：把「缺东西不是结论，是要做的工作」写成规则；仓库注释/README/记忆里写着
     「本机没有它」只能当线索，不能当证据；确实装不上才收尾，且必须写清试过什么、报错是什么。
  5. **补齐之后，验证证据必须走 run_tests（2026-11-11 真模型读数追补）**：上面两轮里模型都把
     `set "PATH=…" & npm test` 放在 `run_shell` 里跑通，交付写「测试通过」；验证闸门只采信
     `run_tests`（最后一次是失败/跳过），于是两轮都被判「交付文本声称测试通过，实际验证结果
     不支持」（task `d59fe7a1…`、`eb4f384a…`）。闸门没判错，是模型不知道证据通道：系统提示词
     的环境缺口段、`run_tests` 的 skipped 回执、`remediation.hint()` 第 3 步三处原来都只写
     「加进 PATH 重跑」。现在三处都写明「用 run_tests 重跑（可传 command 覆盖，把 PATH 前置写进
     同一条命令）；run_shell 里跑通不算验证证据」。
  测试：新增 `tests/test_agent_remediation_v24.py` 13 例（真实交付原文 + 真实 `where node` 回执做
  输入，真实 AgentSession/网关跑执行节点），`test_stall_v151.py` 44 例、`test_intent_and_reply.py`
  37 例，合计 94 例全绿；变异 4/4 全红（拆闸门 / 答复不再交回 / 把确认算成安装 / 去掉意图覆盖）；
  第 5 条另做变异 3/3 全红（分别删掉提示词 / skip 回执 / 补救指令里的通道句）。
  真模型点验（2026-11-11，`:8061` + 客户端 DeepSeek Key）：催办两轮均判 `intent=act`；
  `run_tests` 回执 `skipped`（本机没有 npm 工具链）；模型自己 `where` →
  `winget install OpenJS.NodeJS.LTS`，真装上 Node v26.13.0 / npm 11.20.0，再跑到 `npm test`
  退出码 0——不再停在「需要你装」。第 5 条修复后复跑（task `f33bc6c4…`）：`run_tests` 先
  `skipped` → winget 安装 → `run_tests {"command": "set \"PATH=C:\Program Files\nodejs;%PATH%\" && npm test"}`
  → `status=ok`、`validation.completed passed=true`、`verification.verdict passed=true`、任务
  `succeeded`，交付里带命令原文与退出码。后端全量 2574 passed / 24 skipped（852.67s，rc=0）。

- **沙箱临时区容量上限：固定容量 VHDX 卷 + 写满 ENOSPC 行为探针（J2）**：NTFS 没有目录级
  容量配额（`fsutil quota` 只按用户/卷记，按用户设会误伤同一用户的工作区写盘），所以容量上限
  落到**独立卷**上：`create vdisk maximum=N type=expandable` + `attach` + 分区 + `format fs=ntfs
  quick` + `assign mount=<目录>`，每次执行在卷上建独立临时目录并把它设成子进程的 `TEMP`/`TMP`/
  `BLANK_SCRATCH`。只约束临时区，**不约束用户工作区**——工作区容量仍冻结（见「未验证」第 3 项）。
  容量上限由 `SandboxPolicy.disk_mb`（`BLANK_SANDBOX_DISK_MB`，默认 2048，0=关）控制，卷与
  挂载点默认放 `%ProgramData%\blank-quota`（可用 `BLANK_SANDBOX_DISK_ROOT` 改）：受限令牌/AC
  子进程要能**穿过**上级目录才能用挂载点里的临时目录，而 `%LOCALAPPDATA%\Temp` 的 ACL 不给
  Users 遍历权；非管理员退回 `%TEMP%\blank-quota`（挂载本身需要管理员，失败只记账不静默）。
  探针是行为证据：挂一个 64MB 卷、按 4MB 块写满再追加，必须拿到 `OSError errno=28`
  （WinError 112/39）且**第二次写仍失败**才算通过；收口当刻实测（管理员，build 26300）：
  容量 62.93MB、写入 48MB 后 free 3.14MB、`errno=28`、`second_write_failed=true`，耗时 3.8s。
  执行层账本新增 `disk_enforced` / `disk_bytes` / `disk_error`：卷挂载成功、本次临时目录真的
  落在卷上、探针证明写满会失败，三条同时成立才写 True；docker 命令行没有容量参数，容器档恒
  False。受限令牌与 AppContainer 后端的 `prepare` 同时给临时目录授 `M`、给卷根与上级目录授
  `RX`（原先受限子进程根本写不进 TEMP，这一条是接线时真机抓到的）。`PROBE_VERSION` 升到 5；
  app 启动 `warm_boundary()` 在开档且非 docker 时后台预热容量卷；治理快照带 `sandbox.disk_mb`
  与 `enforced.disk`，面板按 `disk_mb → disk` 查账并显示成 `2048m`，未证明时标「未强制」。
  新增 `tests/test_disk_quota_v23.py` 22 例（策略解析、卷缓存与失败不重试、临时区回退、
  假容量卷/非管理员/挂载失败三种负例、真实 64MB ENOSPC、三后端账本真值表、网关白名单、治理
  快照、真实 LocalSandbox 与真实受限子进程写 TEMP、悬空挂载点回收与孤儿清扫）；关联六套件
  （`test_disk_quota_v23` 22 + `test_boundary_facts_v20` 18 + `test_cpu_quota_v169` 11 +
  `test_io_rate_v21` 12 + `test_appcontainer_v22` 29 + `test_governance_snapshot_v16` 13）
  105 例全绿。变异 3/3 全红：拆掉 `scratch`、`cap.disk_quota`、`volume.ok` 任一条件，账本判据
  分别变红；其中 `volume.ok` 第一次**没红**——挂载失败用例传了空 scratch，被另一个条件遮成
  死判据，改成真实形状（临时区退回宿主目录、scratch 非空、卷 ok=False）后才可失败。
  回收路径也抓到一个真缺陷：卷卸下后挂载点变成**悬空目录联接**，`Path.exists()` 跟随联接返回
  False，旧实现整段 `rmdir` 被跳过，本机实测在 `%ProgramData%\blank-quota` 留下 16 个空壳；
  改用 `lexists` 收联接，并让 `_sweep_stale` 顺带清死进程的孤儿挂载点。判据
  `test_detach_removes_a_dangling_mount_junction` 在改回 `exists()` 时实测变红。

- **CPU 限速（Job CpuRate + 行为探针）**：`SandboxPolicy.cpus` 从"只是配置值"变成内核真限。
  Job 通过 `JobObjectCpuRateControlInformation`（ENABLE|HARD_CAP）设限；`CpuRate` 的单位是
  **整机算力的万分之一**（16 核：625 = 1 核，10000 = 整机），不是"单核的百分比"——第一版探针
  按后者读，2000/5000 两档在单线程上毫无变化。换算进 `windows_job.cpu_rate_for`；`cpus`
  不小于整机核数时不设限，并在 `cpu_error` 里说清"没构成限制"，不许记成已强制。
  账本要求两个都真：本次 Job 真接受了 `cpu_rate`，且 `_probe_cpu_rate` 现场压过——同一负载
  （4 个自旋子进程 × 0.5s）跑两轮，对照必须跑满 `workers×seconds×0.4`（装置自检），限速侧
  必须 ≤ 对照的 70%；单核机器构造不出对照，如实报"测不了"而不是猜成支持。收口当刻实测
  （非管理员，build 26300，16 逻辑核）：对照 1.984 CPU 秒 → 限速侧 1.016，阈值 1.389，
  `cpu_rate=625`。`cpu_enforced` 与 `enforced_fields` 的 `cpu` 同口径；生效时 notes 写
  "CPU 上限由 Job CPU rate 强制（整棵进程树合计，按整机算力折算）"。`PROBE_VERSION` 升到 2
  （探针语义变了，进治理指纹）；面板 `job_only` 文案从"只有内存/进程数/进程树"改为
  "资源与生命周期由内核强制"，cpus 只有在 `enforced.cpu=true` 时才不再标「未强制」。
  新增 `tests/test_cpu_quota_v169.py` 11 例，关联四套件 45 例全绿；变异 10/10 全红且红点
  对得上（整机换算、整机核数边界、LocalSandbox 接线、local/restricted 两处账本、机器账本
  的 Job 前提、探针结论抄写、notes、对照自检、限速阈值各至少 1 例）。

- **AppContainer 后端：Windows 上的文件与网络边界由内核拒（需管理员）**：
  `BLANK_SANDBOX_APPCONTAINER=1` 打开，`runner.get_sandbox` 的选择顺序是容器 → AppContainer
  → 受限令牌 → local。固定包名 `BlankSandbox`（`CreateAppContainerProfile`，已存在则
  `DeriveAppContainerSidFromAppContainerName`），`CreateProcessW` + `STARTUPINFOEXW` 挂
  零能力包 SID 与三句柄白名单，`CREATE_SUSPENDED` → 挂 Job → `ResumeThread`。
  授权只给工作区/临时目录 `M` 与工具链根 `RX /T`；祖先路径不授权——AC 令牌保留
  `SeChangeNotifyPrivilege`，实测用全新包名、零祖先授权时 AC 内 Python 照常启动
  （`E:/tmp/i-probe/no_ancestor.py`），而给祖先写 DACL 会让自检从 3.9s 涨回 200s+。
  准入是**行为证据**而非能力推断：同一个 AC 子进程里量到 AC `{'bind':'ok','loopback':'ok',
  'external':'PermissionError:13','secret':'PermissionError:13','ws_write':'ws-ok'}`、
  宿主对照 `{'external':'ok','secret':'SYNTHETIC-NOT-A-KEY',...}`；任何一条对照红掉整次
  判定作废（`appcontainer_unverified`），后端不会被选中。账本里 `filesystem_enforced`
  与 `network_enforced` 只读这份结论；`workspace-net` 在 AC 下明确拒绝（零能力包的方向与
  "要联网"相反，且不许降级成 off）。`boundary.PROBE_VERSION` 升到 3，新增
  `appcontainer_file_boundary` / `appcontainer_network_boundary` / `appcontainer_check`
  三个事实字段；`doctor` 先 `self_check(force=True)` 再取边界账；设置页与任务面板把
  `isolation=appcontainer` 单列为一种隔离（不再印成宿主直连）。
  一个必须修的差异：cmd 对**未加引号的路径型首词**会走搜索解析，AC 令牌缺目录访问权时
  报「拒绝访问。」；同一命令在宿主与受限令牌后端正常（`E:/tmp/i-probe/restricted_quote.json`）。
  隔离实验证明只有首词引号决定成败（`E:/tmp/i-probe/ac_quote_isolate.json`：`"exe" 参数`
  成功、`exe 参数` 失败、`"exe" -c print(1)` 正常）。新增 `quote_leading_path` 只给路径型
  首词补引号（裸名 `npm`/`python` 不动，否则会关掉 PATH 搜索），四种命令形状（字符串/argv
  列表 × 脚本文件/`-c`）在真实执行层全部 exit 0，读数一致（`E:/tmp/i-probe/ac_runner_e2e.json`）。
  新增 `tests/test_appcontainer_v22.py` 29 例（judge 真值表与对照优先级、引号归一化、
  `enforced_fields`/档位两条边界分开记、enforcement 账本、选择门、workspace-net 拒绝、
  治理与设置页文案）；关联八套件 165 例全绿；变异 10/10 全红（含"缺自检结论当全绿"与
  "网络事实顶替文件事实"两条补靶）。

- **Job IO 速率上限：接进执行层，由行为探针现场证明（J1）**：上一轮冻结的结论
  「接受设置但不节流」是**假阴性**——`JOBOBJECT_IO_RATE_CONTROL_INFORMATION` 里多插了
  一个 `MaxBandwidthStrict`，`ControlFlags` 落到结构外，`SetInformationJobObject` 返回 True
  但限速根本没开。按 48 字节 `NATIVE_V1` 布局重写后，2026-11-10 在本机（管理员、build 26300）
  用真实执行形状（父进程建 Job → 起子进程 → assign → 持续写盘）复测：对照 680.9 MB/s；
  请求 16/32/8 MB/s 时限速侧实测 16.1/32.5/8.0 MB/s（误差 <1.5%）。`SandboxPolicy.io_bps`
  （`BLANK_SANDBOX_IO_BPS`，默认 `128m`，0=不设）经 `windows_job.create_job(io_bps=...)`
  设进 local / 受限令牌 / AppContainer 三个后端；执行层账本新增
  `io_enforced` / `io_bps` / `io_error`：Job 接受 `io_bps` **且** `_probe_io_rate` 现场
  证明节流，两个都真才写 True；docker 命令行没有带宽参数，容器这一档恒 False。
  探针固定请求 16 MB/s、写 32MB：对照必须 ≥ 2× 限速档（否则如实报"构造不出对照"），
  限速侧 ≤ 对照×0.7 才算通过；收口当刻读数：对照 507.9 MB/s、限速侧 18.8 MB/s、阈值 355.6。
  `PROBE_VERSION` 升到 4；`boundary.enforced_fields` 从 7 项扩到 8 项（新增 `io`），
  治理快照带 `sandbox.io_bps`，面板按 `io_bps → io` 查账并把 134217728 显示成 `128m/s`。
  探针进程内只跑一次（CPU ~3s + IO ~2.5s），app 启动已在后台线程 `warm_boundary()` 预热。
  新增 `tests/test_io_rate_v21.py` 12 例；变异 4/4 全红且红点对得上：LocalSandbox 接线
  删掉（账本 False）、`ControlFlags` 清零（复现旧假阴性：Set 成功、探针报
  "限速未生效：344.1 > 阈值 240.9"）、`enforced_fields` 丢掉探针证据（Job 在就点绿）、
  面板漏掉 `io_bps → io` 映射。范围：只覆盖命令执行；fs-broker 的文件工具与 MCP 长驻进程
  未纳入；磁盘**容量**配额仍冻结（见下方「未验证」第 3 项）。

- **远程 MCP over OAuth 2.0（发现 → client_credentials → 刷新）**：`mcp.json` 的 `auth`
  段从"只有静态 headers"扩到真令牌生命周期。只走无人工的 `client_credentials` +
  `refresh_token`：桌面/后端进程里没人能点浏览器授权页，`authorization_code` 明确拒绝
  （`MCPStatus.skipped` 带原因），不假装支持。发现按 MCP 授权规范走两跳：RFC 9728
  受保护资源元数据（`/.well-known/oauth-protected-resource`，路径感知候选 + 同源兜底）
  给出授权服务器，RFC 8414 / OIDC 元数据给出 `token_endpoint` 与
  `token_endpoint_auth_methods_supported`；元数据的 `issuer` 与发现到的 issuer 不一致就拒。
  非 loopback 一律要求 https（只有 `http://127.0.0.1`/localhost 放行），令牌与发现请求都走
  `trust_env=False` 的客户端——环境里的代理变量不该把 Bearer 令牌捎到别处。
  令牌侧：`client_secret_basic` / `client_secret_post` 两法（显式 `token_url` 时也按
  `auth_method` 生效）；RFC 8707 `resource` 绑定目标服务端。JWT **只解包、不验签**——
  令牌是刚经 TLS 从令牌端点取回的，本机要核的是"是不是发给我、是不是还活着"：`aud`
  缺失或不符一律拒（期望值与实际值都写进错误），`exp` 已过拒，未写 `expires_in` 时用
  `exp - now` 调度，提前 30 秒刷新；opaque 令牌无法在本机核对，状态里如实写
  `audience_checked=false`，靠服务端 401 驱动。401 后只刷新重试一次（先 `refresh_token`，
  被拒退回 `client_credentials`），不做刷新风暴。令牌、刷新令牌与 client_secret 不进错误
  文案（`_redact`）、不进状态/证据（`facts()` 只有类型、剩余秒数与核对结论），
  `OAuthConfig.client_secret` 的 `repr` 已关。新增 `tests/test_mcp_oauth_v170.py` 24 例
  （假授权服务器覆盖发现两跳、两种客户端认证、受众/过期/opaque、refresh 被拒回退、
  401 只刷一次、令牌不入状态与错误）。测试当场揪出三个实现缺陷：显式 `token_url` 配
  `auth_method=post` 会静默退回 Basic；同一秒内签发的 JWT 逐字节相同，"拒绝第一枚"
  会把刷新后的新令牌一起拒掉（改每个测试令牌带唯一 `jti`）；`close()` 抹掉令牌后
  再读 `facts()` 会 KeyError。

- **更新源分离签名（Ed25519）与 CycloneDX SBOM**：原来的更新只校验 `sha256`——摘要与
  feed 来自同一个源，源被攻陷时两者可以一起改。现在 feed 条目必须带 Ed25519 **分离签名**：
  签名覆盖 `{version,url,sha256,size,sbom}` 固定顺序、无空白的规范字节
  （`canonicalUpdatePayload` 由壳端与发版脚本共用），`size` 必须是正整数、摘要统一小写。
  公钥钉死在 `desktop/shell-policy.cjs`（`UPDATE_PUBKEY_PEM`，Ed25519），私钥只在发版人
  手上、不进仓库；未打包时允许 `BLANK_UPDATE_PUBKEY_PEM` 注入公钥供接线测试，
  `app.isPackaged` 为真时这条旁路关闭。`verifyUpdateSignature` 对缺签名、缺 size、坏
  base64、非 64 字节、公钥不可解析各给可读原因且绝不抛。`main.cjs` 两道闸：
  **下载前**验签（不过就不取包、托盘写「更新：不可用」、不留安装入口），**装前**再验一次
  签名并按实际字节重算摘要。`scripts/make_update_feed.mjs` 现在必须给
  `--sign-key`/`BLANK_UPDATE_SIGNING_KEY`，自检含"改任一被签字段必拒"与"未签名必拒"，
  私钥与壳内公钥不配对时当场失败、不产出装不上的 feed。
  随包 SBOM 由 `scripts/make_sbom.mjs` 生成 CycloneDX 1.5：npm 组件逐条来自
  `desktop/package-lock.json` 的 `packages`（含传递依赖），Python 组件来自
  `services/api/uv.lock`；serialNumber 由组件集合哈希派生（同组件集合同序号）；
  `metadata.properties` 写入安装包文件名、sha256 与字节数，SBOM 与二进制能对上号。
  自检当场揪出一个口径错误：同名同版本在锁文件里因嵌套 `node_modules` 出现多次
  （392 条 purl 只有 344 个唯一值），组件是"库"不是"安装位置"，改为按 purl 去重后才写出。
  实测（2026-11-10，真产物 `desktop/dist/Blank-AI-安装版-0.11.2.exe`，131457034 字节
  sha256=`3746586e…bbf40`）：SBOM 344 个组件（npm 272 + pypi 72）自检全过；用仓库外私钥
  签发 feed 后，壳内钉住的公钥验签通过，改 version/url/sha256/size/sbom 任一项都被拒，
  未签名条目被拒，`planInstall` 对换包（摘要全 0）被拒；缺安装包时脚本退出码 2。
  桌面套件 101 例全绿（新增 `update-policy` 6 例 + `main-wiring` 1 例：unsigned/badsig
  必须在下载之前拒绝）；关键路径变异：撤掉下载前验签，接线用例立刻变红。
  仍未落地的是 exe 的 Authenticode 证书（`sign:check` 退出码 2，只出未签名包），
  见下方「未验证」第 5 项。

- **按句柄复验路径，堵住预检与 open 之间的窗口**：`resolve_inside` / `resolve_for_write`
  判定的是路径字符串，等真正 `open` 时中间目录可能已被换成指向工作区外的 junction
  （Windows）或符号链接（POSIX）。新增 `workspace.open_verified`：先 `os.open`，再按
  **句柄**取最终路径复验——Windows 走 `GetFinalPathNameByHandleW`（剥掉 `\\?\` 与
  `\\?\UNC\` 前缀），POSIX 走 `realpath` + `(st_dev, st_ino)` 与 `fstat(fd)` 比对；
  拿不到最终路径一律拒绝（「验不了」不等于「没问题」）。新建用 `O_CREAT|O_EXCL`，
  任何打开方式都不带 `O_TRUNC`——截断必须发生在复验之后，否则链接会先毁掉外面的文件；
  复验失败时关句柄并删掉自己刚建的那个空文件，外面不留痕。`write_verified` 先试
  `r+b`、不存在再 `wb` 新建，写入都落在复验过的同一个句柄上。接线到
  `read_text_file`、`_patch_on_disk`（读与写共用同一个复验过的句柄，中间没有路径可换）、
  `write_backup`、`restore_backup`。二进制句柄不做换行转换，补丁读写两侧都包
  `io.TextIOWrapper(..., newline=None)`，与原先 `read_text`/`write_text` 口径一致
  （CRLF 读成 LF、写回本机换行），否则 CRLF 仓库里所有补丁都会匹配失败。新增
  `tests/test_path_verification_v168.py` 6 例：先造真目录（预检能过）、换成链接、
  再 monkeypatch 掉预检，只看句柄层拦不拦得住——读、补丁改写、补丁新建、
  备份写四条必须 `path_escape_denied` 且外部内容/文件不变，外加正常读与
  补丁回滚两条对照。关联 34 个套件 449 passed / 14 skipped。变异：关掉复验判定
  → 4 条链接用例红、2 条对照绿；撤掉读接线 → 1 红；撤掉补丁读接线 → 1 红；
  撤掉备份接线 → 1 红；还原后全绿。

- **受限子进程只继承三个标准句柄**：`bInheritHandles=True` 是 stdout/stderr 重定向的前提，
  但它的默认语义是「宿主里所有带继承标志的句柄全递」——浏览器管道、打开的文件、套接字都会
  漏进被沙箱化的一侧。启动改用 `STARTUPINFOEXW` + `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`，
  继承范围压到 NUL/stdin/stdout/stderr 三个；属性表建不出来（`InitializeProcThreadAttributeList`
  或 `UpdateProcThreadAttribute` 失败）就**拒绝启动**，不退回「继承全部」。属性表缓冲区与
  句柄数组都活到 `CreateProcessAsUserW` 返回（缓冲区存的是数组地址，提前回收就是悬空指针）。
  本机实测（build 26300，非管理员）：宿主摆 24 个可继承文件句柄，受限子进程句柄数
  96 → 96（差值 0）；撤掉属性表后 96 → 120（正好多 24）。新增 1 例
  `test_child_inherits_only_the_three_stdio_handles`（父侧 Toolhelp 找到子 cmd，再读
  `GetProcessHandleCount`；判据是基线与加料两次的差值 ≤ 4），受限令牌套件 14 例全绿，
  关联 17 个沙箱/边界/权限套件 246 passed / 14 skipped。变异：撤掉白名单 + 去掉
  `EXTENDED_STARTUPINFO_PRESENT` → 仅该 1 例红，还原后全绿。

- **边界来源证明与降级告警**：治理快照现在能回答「这一步被哪一版手写规则、哪一版探针
  量出来的」。手写规则新增稳定指纹 `ruleset.sha`（按生效规则与顺序计算，注释/空行不动它；
  坏文件整份停用、指纹留空，与「文件为空」区分）；`boundary.PROBE_VERSION` 进
  `Capability`、快照字段 `boundary_probe_version` 与治理指纹——证据包里能把「机器变了」
  和「量它的尺子变了」分开。边界变弱单独写 `boundary.downgraded` 事件：强度档位下降、
  装置自可信变不可信、后端从有变无、任一 enforced 字段 True→False、档位可用→不可用，
  每条原因带前后值；升级不告警，同样的弱边界不重复告警。证据包 `governance` 新增
  `rules_sha` 与 `boundary_downgrades{count, events[seq, step, node, reasons]}`。
  探测节奏沿用现有口径：每个模型调用前现查，探针缓存 TTL 60 秒——**不是**独立定时器，
  这一步没跑模型就不会复查。新增 `tests/test_boundary_attestation_v167.py` 13 例，
  关联五套件 82 例全绿。变异 5/5 全红且红点对得上：指纹不进快照 → 2 例红，关掉降级判定
  → 5 例红，不写降级事件 → 2 例红，探针版本写死 → 1 例红，证据包不装载降级事件 → 1 例红。

- **命令级策略（execpolicy，对标 Codex rules）**：规则与会话批准/驳回不再停在外层命令首词。
  壳包装的内联载荷参与匹配：`cmd /c|/k`、`powershell|pwsh -Command|-c`、`sh|bash|zsh|dash -c`
  递归展开（深度上限 4、片段上限 64），脚本路径（`bash script.sh`）不展开——展开脚本等于替用户
  批准脚本里的任意内容。deny 改成「任一段命中就拒」：`echo hi && rm -rf build` 里真正的删除
  不能再搭无害段的便车；对 `\"`、`` `" ``、`^"`、`""` 多加一个去引号视图，`powershell -Command
  "cmd /c \"rm -rf build\""` 这种转义形状也能拒到，去引号视图只追加在 deny 侧，只收紧不放宽。
  allow 仍要求每一段都被覆盖：`cmd /c "git status"` 可放行，`cmd /c "git status && rm -rf src"`
  不行；旧形状的整串规则（`allow run_shell(cmd /c "git status")`）照样有效。会话驳回同口径
  （任一展开片段命中即拒）。新增 `BLANK_SHELL_POLICY=allowlist`：没有命中显式规则/会话批准/工具档的
  `run_shell` 一律回到人手；默认 `ask` 保持产品现状，排在全放行档与 `approve_high_risk` 之后——
  这两者本身就是用户/调用方当场写下的授权。新增 `tests/test_exec_policy_v166.py` 18 例，关联七个
  权限套件 194 例全绿。变异 4/4 全红且红点对得上：撤掉壳展开 → 9 例红（含两条端到端），
  deny 退回逐段全覆盖 → 6 例红，撤掉去引号视图 → 仅转义形状那 1 例红，关掉 allowlist 闸 → 仅
  默认拒绝档那 1 例红。端到端正反对照：同一 `cmd /c "del src\core.py"`，deny 规则下文件还在、
  allow 规则下文件真被删——红不是「命令本来就跑不起来」。

- **受限令牌后端（Windows，默认关）+ 文件边界成为独立事实**：`BLANK_SANDBOX_RESTRICTED=1` 时，
  Docker 不可用且自检全绿就改用 `CreateRestrictedToken`（限制 SID = Users / Everyone /
  Authenticated Users / 登录会话 SID）+ Job 启动命令。文件边界由 NTFS 真的拒；网络没有
  AppContainer 就拦不住，账本固定 `network_enforced=false`——拿受限令牌冒充网络隔离，就是本立项
  要消灭的那类谎。准入自检不是装饰：同一个受限子进程里必须同时出现「合成私钥读不到」与「工作区
  读写成功」，另加宿主令牌、授权后、去掉限制 SID 三条对照；任一对照红掉，整次判定作废、后端不许启用。
  规则文件（`BLANK_SANDBOX_RULES_FILE`，不设则用包内 `rules.default.json`）只接受 `default_action=deny`，
  任何覆盖 `~/.ssh` 等保护路径的授权会让整份规则作废，未知字段或空占位符直接报错。
  Job 改为 `CREATE_SUSPENDED` → 按句柄挂入 → `ResumeThread`，消掉「按 PID 再 OpenProcess」的竞态。
  2026-11-09 本机（非管理员，build 26300）实测：`BLANK_SANDBOX_RESTRICTED=1 blank doctor --sandbox`
  → 拦住 6 / 未强制 1（P-NET 网络）/ 不确定 0，八条账全部一致、退出码 0；同一命令用默认 local 后端
  → 拦住 2（P-PROC / P-INHERIT）/ 未强制 5（P-READ / P-WRITE / P-ENV / P-NET / P-ESCAPE）。
  三个实测坑写进实现：受限令牌下 cmd 拒绝执行 `.cmd`（改 inline `/d /s /c`）、
  `DETACHED_PROCESS` / `CREATE_NO_WINDOW` 让 Python 起不来（0xC0000142，改 flags=0 + `SW_HIDE`）、
  `icacls /T` 配 `(OI)(CI)` 对文件静默跳过（改根目录 + 逐文件两段式授权）。
  账本拆分：`local` 不再借机器能力，文件/网络恒 False、来源写 `measured_job_object`；受限令牌后端的
  `filesystem_enforced` 来自行为自检，`network_enforced` 恒 False。新增 30 例（规则 16、受限令牌与后端
  选择 13、事件循环不阻塞 1）；后端全量 **2360 passed / 24 skipped / 0 failed**（798.84s，`EXIT=0`）。
  边界探测原本把行为自检同步跑在事件循环里，六个进程并跑时首条命令从 6-9s 拖到 22-24s；现在 `local`
  与 `restricted` 两条算账路径都走线程，新用例用会睡 0.6s 的替身数事件循环空转次数（撤掉 `to_thread`
  即红：0 次 < 10 次）。
  收口说明：改动期三次全量各有一次团队轮次用例在 30s 内没等到收尾（origin 批准续跑 / midround 降上限 /
  run_id 形状，每次只红一个）；这些用例单跑、同文件子集与 HEAD 对照均绿，插桩显示行为自检只在进程早期
  跑了一次 0.24s，因此按「团队夹具在共享临时库上的时序敏感」记录在案，没有调大超时。上面这轮全量干净。

- **代码签名接线（待证书）**：`desktop/package.json` 的 `win.signtoolOptions` 写死发布者与证书主体
  「无锡空白格人工智能科技有限公司」、`sha256`、RFC3161 时间戳；新增 `npm run sign:check` 只读自检
  （0=证书就绪 / 2=本机没有该公司证书 / 1=配置或查询有问题）。本机实测退出码 **2**：证书库里按代码签名
  用途过滤后为 0 张，也没有 `.pfx/.p12/.cer`——私钥不在仓库里，正式分发前必须装证书或用 `CSC_LINK`。
  `dist` 改走 `dist-build.cjs`：配了 `certificateSubjectName` 而不加这层时，electron-builder 会去证书库找、
  找不到直接失败（本机实测 `Cannot find certificate 无锡空白格人工智能科技有限公司`、退出码 1）；现在
  有证书按指纹签、配了 `CSC_LINK` 交给外部证书、都没有则只关签名并明说「本次只出未签名包」。
  同一棵树上 `node dist-build.cjs dir` 实测退出码 0、产出 `dist/win-unpacked/Blank AI.exe`（未签名）。
  三态覆盖项与「覆盖项真的能清掉主体名」用 electron-builder 自己的 `deepAssign` 钉住，变异 3/3 全红。
- **Go 项目缺工具链不再被当成代码失败**：`run_tests` 识别 `go.mod` 后先探测 `go` 是否在 PATH；缺工具链时
  返回 `status=skipped` 并带上「本机没有 go 工具链」，验证节点把它落成带理由的 `validation.completed`，
  交付文案会明说未验证。2026-11-09 复测（`evals/results/results-2026-11-09T04-47-06-167Z.json`）：
  T11/T28 的 `validation.status=skipped`、`unverified_risks` 写明缺工具链，**不再空转修复回路**；
  但任务最终仍是 `failed`——脚本化桩的固定交付文案照样声称「测试通过」，`no_false_claim` 按实判红
  （`failure_reason=交付文本声称测试通过，实际验证结果不支持`）。门禁本机 **0.875 < 0.938**
  （红 T07/T29 `files_missing` 外加 T11/T28）；阈值没动，评测机装 Go 后才会回到 0.938。
- **桌面 README 纠偏**：自动更新段落按现状改写（检查 → 下载 → 校验 → 用户点击安装），不再声称"不下载不安装"。

### 未验证（需外部条件，不计入「已完成」）

以下六项是本轮内核升级里**没有**落地或**没有**在本机真验的部分。冻结在这里，不写进
「已完成」；每项都留下判据、红了长啥样与哑弹风险，条件具备时照着验。
（CPU 限速、IO 速率上限、远程 MCP 的 OAuth、更新源分离签名与 SBOM 已从这张表移出，见上方条目；
Windows 网络出口强制已由 AppContainer 落地，见上方条目与第 1 项的剩余部分；第 2 项
Linux 原生后端按项目决定移出范围，判据只作留档；任务级隔离主体与任务根容量卷已由 J3 落地，
第 3、4 项只剩共享工作区配额、多任务卷并发、另一 OS 用户与链接绕过这几格。）

1. **网络出口强制（P0）**——Windows 已落地（上方 AppContainer 条目：零能力包、
   同进程内对外 TCP 被拒为 `PermissionError:13`、宿主对照在同一时刻外连成功、
   账本只读这份行为证据），**仍未成立的是**：`local` 与 `restricted` 后端（未开
   AppContainer 或没有管理员权限时）账本恒 `network_enforced=false`，面板如实标未强制；
   WFP 式的「保留宿主文件访问、只拦出口」没有实现。Linux 路径按项目决定不再做。
   剩余判据：local/restricted 下账本必须为 false、doctor 必须报未强制，不许借
   「机器能建 AC」冒充本次已强制；若将来接 WFP，同一个子进程内回环监听连接成功、
   对外 TCP 被拒且账本为 true，撤掉规则后对外连接必须成功（变异对照）。红了长啥样：
   local 档账本写 true，或只在开启 AppContainer 的机器上把开关关掉仍报已强制。
   哑弹风险：WFP 规则绑错进程（只拦父不拦子）、Windows 版本差异导致过滤器静默不生效、
   把「AC 内某一次外连失败」当成「所有出口都被拦」（IPv6、代理、原始套接字未逐一验）。
2. **Linux 原生沙箱后端（P1，已移出范围，仅留档）**——项目决定不做 Linux 场景；
   CI `api` job 仍在 Ubuntu 上跑全量，但那只是回归，不构成 Linux 上的边界承诺——
   代码里没有 landlock/seccomp/unshare，Linux 上非 Docker 后端不提供本机文件/网络边界。
   若将来重启：需要 Linux 内核 ≥ 5.13（Landlock）或可用的非特权 user namespace；判据是
   在 `ubuntu-latest` 上，子进程写工作区外被内核拒（EACCES/EPERM，不是路径预检返回的
   4xx）、对外连接被拒、自检阴性/阳性对照都成立。红了长啥样：探针只走路径字符串判断、
   或内核不支持时用例 `skip` 却被当成通过。哑弹风险：runner 关闭非特权 userns、Landlock
   ABI 低于所需版本、受限令牌代码被错误地当成跨平台后端。
3. **工作区磁盘容量配额（P2）**——沙箱**临时区**（TEMP/TMP/`BLANK_SCRATCH`）的容量上限
   已在本轮落地（见上方 J2 条目：固定容量 VHDX 卷 + 写满 ENOSPC 行为探针，账本
   `disk_enforced` 与探针同源）；**任务根**的容量上限随 J3 落地（`BLANK_TASK_DISK_MB`，
   生产路径探针 64MB 卷写到 53,477,376 字节后内核返回 `errno=28`，释放后无 vhdx/挂载点
   残留）；仍冻结的是**共享工作区**（未开任务隔离）的容量上限：命令往工作区写盘不受任何
   卷约束，`disk` 只承诺临时区与任务根。需要：把固定容量卷覆盖到工作区，并做**多任务并发**
   卷生命周期回收（本机只验了单任务）。判据：写盘到配额上限后继续写失败且总占用不再增长
   （ENOSPC，不是我们的路径预检）；detach 后挂载点可删、无残留卷。红了长啥样：工作区磁盘
   无限增长、失败来自路径闸门而不是内核、卷 detach 不掉。哑弹风险：卷名/挂载点在异常退出后
   残留、多个任务共用一个卷时互相写爆、把「写满报错」当成「文件边界」写进 `filesystem_enforced`。
4. **任务级隔离（P3）**——**已落地**（见上方 J3 条目）：每任务独立根 + 唯一 AppContainer 包，
   同机**另一任务包**用绝对路径与 `..\` 相对路径读写都被内核拒（`PermissionError:13`），
   宿主对照可读。**仍冻结的两格**：同机**另一个 OS 用户**的不可达性未实测（当前只验证了同一
   用户下的不同包身份）；junction/符号链接/硬链接绕过未做行为探针。判据：换一个 OS 用户跑
   同样的读取必须被拒；在任务根里建指向家目录的 junction 后读写仍被拒。红了长啥样：另一个
   包/用户用绝对路径照样打开、链接把 ACL 检查绕到目标路径之外。哑弹风险：工作区根互相嵌套、
   把「路径闸门拒绝」当成内核拒绝、只在同一 OS 用户令牌下自证。
5. **Authenticode 代码签名与干净机装包复核（P3）**——更新源的**分离签名**（Ed25519）与
   CycloneDX SBOM 已在本轮落地（见上方条目：下载前验签、装前复验、篡改任一被签字段即拒；
   SBOM 由锁文件生成并带上安装包摘要）。仍冻结的是 exe 本身的 Authenticode 签名：配置已
   接线（发布者=无锡空白格人工智能科技有限公司），但本机没有证书（`sign:check` 退出码 2，
   只出未签名包）。需要：真实代码签名证书 + 一台干净机器。判据：干净机器上
   `signtool verify /pa` 通过；篡改安装包后更新被拒（本机已用真产物与真私钥验过，见上）；
   Windows SmartScreen 不再报未知发布者。红了长啥样：未签名包可安装、签名坏掉仍继续、
   证书过期后旧包验不过（缺 RFC3161 时间戳时）。哑弹风险：证书主体与发布者名不一致、
   时间戳服务不可用导致签名静默降级、开发构建绕过校验。
6. **第二台机器差异验证（#76，外部机器）**——现状：边界对账装置已进仓库，但只在本机
   （build 26300）跑过，权限覆盖非管理员与管理员两档；Windows 版本、ACL 语义、
   AppContainer/受限令牌可用性差异没有实测。需要：第二台干净机器（建议不同 Windows 版本）。判据：同一装置在第二台
   机器上输出逐项一致的边界账（或差异有明确原因），受限令牌后端自检全绿才启用。红了长
   啥样：第二台机器上后端自检假绿、文件边界在真实路径上不成立。哑弹风险：只在相同 build
   上复跑、对照项与判定项用同一套代码自证。

## v0.11.2 — 收口「说真话」，接通更新源（2026-11-09）

发布结论：本版不是新功能，是把「看起来通过」逐条换成「真的对得上」。安装包按修复后的源码重打，
提交 `2bd45d8`，已同步到远端 `main`。

- **审批身份闭环**：校验通道与跨恢复续跑同样按 `approval_id + tool_call_id + tool_name + arguments_hash`
  四元匹配，错一个即 409，不再留「旧卡复用」的口子。
- **读图不再静默降级**：`deepseek-flash` 被正确认出支持图片，附件随 `/turns` 进上下文；不支持时明说原因。
- **卡住时说清楚**：任务超时一次打印 13 项现场（任务/运行/派发/供应商/最近事件/解释器/租约…）。
- **触发器边界**：暂停/取消后的状态与按钮一致，watch 根目录之外永不放行。
- **自动修复说真话**：修不好时不再报成功，`verdict` 按实际复核结论给。
- **代理变量不再炸掉所有模型调用**（本版新修）：`NO_PROXY` 里带方括号 IPv6（`[::1]`，curl 的写法）时
  httpx 会把它拼成非法 URL，构造客户端即抛 `InvalidURL: Invalid port: ':1]'`——代理一次没用上，
  所有模型/通道调用先全炸。入口处把方括号归一（httpx 会给裸地址补回方括号），绕过语义不变；
  回归用真子进程钉住，摘掉归一那行即红。因 `src/` 有改动，安装包重打。
- **更新源接通**：`pnpm feed:update` 按壳端契约生成 `latest.json`（版本 + http(s) URL + 64 位 sha256），
  壳端纯函数自检 8 项 + HTTP 全链路 10 步验通；默认不联网，须配 `BLANK_UPDATE_FEED_URL`，安装须用户点击。
- **验收读数**：后端 **2325 passed / 0 failed / 23 skipped**（633.50s，`EXIT=0`）；前端 296 passed，
  `tsc`/lint/构建 `EXIT=0`；安装包自检 **44 项全 ok**；版本一致性 6/6；安装包 sha256 `3746586eb260…`。
- **仍未验证（如实列出）**：CI 未在真 GitHub runner 跑过；#76 第二台机器差异验证未做；
  签名证书未采购（产物 `NotSigned`）；更新源未托管到公网。评测 benchmark 55 例通过率 71%（3 例假完成、1 例未授权高风险）。

## v0.11.1 — 通知通道：「到点了」真的会弹出来（2026-11-08）

- **原生系统通知**：桌面壳直接弹 Windows toast，点一下唤回窗口并跳到那条对话（`?thread=` 深链）。
- **判定仍在后端，壳只负责投递**：要不要打扰由 `agent/notify.py` 判（策略 + 免打扰），结论连同理由写进
  `notify.decided` 事件；`GET /api/v1/notifications` 只吐 `disturb=true` 的那几条，壳不复判。
- **文案按来源分档**：定时起的弹「到点了」，盯梢的弹「有变化了」，审批弹「等你批准」（并出声）。
- **三条降噪**：按 id 去重、窗口正被看着就不弹、一分钟超过 3 条先压着（审批例外）。
- **免打扰入口**：设置 → 执行与智能体 →「通知与免打扰」，填 `22:00-08:00`（跨零点也行），只压「做完/有变化」。

## v0.11.0 — 让 Super Agent 真的会醒（2026-11-08）

- **定时真的到点**：不再登记一条永远不响的待触发；到点在桌面形态下自己起跑（以前只入队、没有 worker 认领）。
- **「盯着工作区」有真实的事件生产者**：每拍扫一次文件 mtime。
- **消息通道做成配置项**：企业微信/钉钉/飞书在「设置 → 消息通道」填完即用。
- **自动更新接上真实下载与摘要校验**：默认不碰网络，须配 `BLANK_UPDATE_FEED_URL`。

> v0.9.1 / v0.10.0 当时未在此文件立条目，概述见 README「更早 / 再上一版」段。

## v0.9.0 — Super Agent Upgrade（2026-11-03）

发布结论：55 项 coding 基线真实跑通（NVIDIA Mistral Large 4.1T，独立评测后端），completionRate 91% / passRate 98% / testPassRate 100% / misEditedFiles 0 / falseDone 0 / falseClaim 0 / unauthorizedHighRisk 0，达全部硬性发布门槛。

- **运行时升级**：Router/Worker 双层架构落地，新增事件触发、后台执行、Watchdog、断点恢复与低打扰交付。
- **触发器与调度快照**：新增 agent 触发器（triggers）与 dispatch snapshot，配套 alembic 迁移 `0031_dispatch_snapshot.py`、`0032_agent_triggers.py`。
- **意图三分法 + 双重预算**：answer/advise/act 判定与步数 + token 双重预算落地，闲聊不再被误判为改代码任务。
- **交付闸门与空转断路器**：写盘前事实闸门（unknowns）与 `_enforce_no_false_claim` 交付闸门上线；空转断路器拦三类浪费回路，拦截用独立 `blocked` 状态、不冒充 `denied`。
- **评测修复**：`run.mjs` 对供应商 429/529 限流归 `provider_failed` 走退避重试（此前静默记 `setup_failed` 漏跑）；评测库 NVIDIA provider 失效 key 已更新。
- **桌面包**：同步 alembic `0031/0032` 迁移，`verify:desktop` 全绿。
- **已知残留**：coding-035 为 fixture 前提与评测口径错位（agent 满足书面 acceptance 但未触碰预期文件），不属能力回退，列入后续整改（修 fixture 或放宽 `expectedFilesTouched` 与 acceptance 的 AND 关系）。
- **未验证项**：真实 Electron GUI 18 项取证因 headless 环境限制待真机补（已如实标注，未冒充）。

## v0.1.0

- 初始 tag（基线）。注：0.1.0–0.8.x 期间为未打 tag 的迭代构建。
