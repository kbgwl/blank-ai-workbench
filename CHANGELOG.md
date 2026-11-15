# 变更日志

## 未发布 — 命令级策略、边界来源证明、句柄最小化、路径复验、CPU 限速、IO 速率上限、临时区容量卷、环境缺口补救、结构化缺口证据与收尾契约、编程纪律、AppContainer 网络边界、签名接线、受限令牌后端与「跑不了测试」如实跳过、任务级隔离、Claude 式编程工具面、场景模式对齐 Claude 执行与编排语义、子代理默认后台与续聊、fork 子代理（Agent(fork)）、后台 shell、任务级工作树会话、Skill 工具（清单 + 按需加载）、ReportFindings 工具、定时工具（Cron + Wakeup）、Workflow 工具（脚本即编排）、NotebookEdit 工具（按单元格编辑）、Monitor 工具（逐行事件流）、REPL 工具（持久 JavaScript 组合）、任务清单 Task*（依赖图与钩子）、MCP 资源四工具（列举/读取/目录/工具热刷新）、LSP 工具（九操作代码智能）、交互问答 AskUserQuestion（多选/片段预览/答案注解）、会话目标提案 ProposeGoal、技能提案 ProposeSkills、文件型子代理定义（`.blank/agents`）、文件型 Skill 定义（`.blank/skills`）、自定义斜杠命令（`.blank/commands`）、输出样式（`.blank/output-styles`）、设置层级（用户 < 项目 < 本机 < 管理策略）、MCP 配置变量展开（`mcp.json`）、插件包（`.claude-plugin/plugin.json`）、MCP 配置三作用域与项目层批准（`.mcp.json`）、子代理审批档/隔离/后台（permissionMode/isolation/background）、settings.json 权限规则（allow/deny/ask）、Skill 的 `paths` 条件激活（conditionalSkills）、钩子事件面第二批（PermissionRequest/Notification/PostToolBatch/Stop/SubagentStop/PreCompact/SessionStart-End）、settings.json 钩子配置与 matcher 语义（hooks/disableAllHooks）、SubagentStart 钩子与 SessionStart 附加上下文、StopFailure 钩子事件（API 错误终结回合）、UserPromptExpansion 与 TeammateIdle 钩子事件、钩子类型六种（command/prompt/agent/http/mcp_tool/script）、思考强度（effort）五档裁决与三路下发、SessionStart 钩子输出四字段（sessionTitle / initialUserMessage / watchPaths / reloadSkills）、agent_transcript_path 补齐（SubagentStop / StopFailure 载荷）、命令/Skill 的 allowed-tools / disallowed-tools 工具池收窄、命令侧 agent/background 派发子代理、命令/Skill 的 `context: fork` 派发闸门、Skill/命令的 `shell` 字段（`!` 块解释器）、Skill 的 `fallback` 薄指针让位（同名后缀插件技能已加载时丢弃）、子代理定义的 `model` 覆盖（`inherit` / 空串 / fork 回落到父会话模型）、子代理定义的 `hooks` 作用域注册（`Stop` 改写为 `SubagentStop`）、子代理定义的 `memory` 持久记忆（作用域 / 目录 / `MEMORY.md` 注入 / `memory_write`）、子代理默认继承 AGENTS.md 指令与 `omitClaudeMd` 关闭开关、子代理派发的 `model` 入参覆盖、子代理工具循环的 PreToolUse / PostToolUse / PostToolUseFailure / PostToolBatch 钩子、工具钩子载荷字段对齐上游（tool_name / tool_input / tool_response / duration_ms / is_interrupt）、子代理模型强制（`CLAUDE_CODE_SUBAGENT_MODEL_FORCE`）、任务级工作树的基准引用（`worktree.baseRef`）、测试通道解释器解析与「命令不存在」归类、收尾交接判据收窄（#376）、UI 对比四处差距（交付文件与入口 / 自测验证通道 / 临时脚本隔离清场 / OWASP 与输出编码）（#379–#383）、评测器同提示词开关（`--plain-prompt`）（#377）、Claude「Doing tasks」六条与无测试配置时的收尾空跑（#384–#386）、交付改动排除 scratchpad 自测脚本（#388）、空响应与零交付不许算成功（#394–#397）

- **UI 对比暴露的四处产品差距：交付文件名与入口 / 自测验证通道 / 临时脚本隔离清场 / OWASP 与输出编码（#379–#383）**：
  同一模型、同一提示词的三方 UI 对比（任务 076ad3a1…）暴露的不是模型能力，而是产品口径：
  我方交付只有「4 个文件」计数，用户按「文件结构 + 怎么打开」无法验收；`node _selftest.js`
  22/22 通过（seq 91），校验通道仍写「未做自动化校验」；模型两次 `del _selftest.js` 被
  guardian 拒绝（seq 100/136），临时脚本留在工作区；前端把分类/日期直接拼进 innerHTML。
  上游取证（`grep -boF` 现场复核偏移 101031256 / 197934220）后逐条补齐：
  1. **交付结构**：`DeliveryFacts` 增 `files`，改动行输出
     `改动：3 个文件（index.html、styles.css、app.js），入口 index.html`；最多列 5 个名字
     （计数照旧），无 HTML 不造入口；`as_dict` 同口径给 `files` / `entry`。事实仍取自工具结果，
     不由模型自述。
  2. **自测验证通道**：新增 `_self_run_validation`，只采信「最后一次写动作之后」的测试型
     `run_shell`（pytest / npm test / node _selftest.js 等，普通 `node app.js` 不算）；
     有退出码且为 0 才记为 `ok`，非零记 `failed` 并进修复回路；证据带 `source="self_run"`。
     `run_tests` 已真跑过时仍以它为先。
  3. **清场不放宽安全策略**：临时命名识别从 `.py` 扩到 js/mjs/cjs/ts/tsx/sh/ps1 与
     `_selftest*`；新增 `quarantine_scratch_files`，只把「本轮新建 + 命中临时命名 + 用户未点名」
     的顶层文件移进 `.blank/scratchpad/`（移动而非删除，事件 `workspace.quarantined`，
     被移走的文件不再计入交付改动）。本轮新建的全是这类脚本时不动——它可能就是用户要的交付物。
  4. **安全编码与可验货**：`CODING_INSTRUCTIONS` 写入上游 OWASP 原句与前端输出编码要求
     （不可信数据先转义或 `textContent`，禁止未转义拼 `innerHTML`；密钥不进代码/日志/交付）；
     交付提示要求文件名、网页入口、怎么打开与真实退出码；`REPORT_INSTRUCTION` 把临时脚本
     指向 `.blank/scratchpad/` 并说明 del/rm 会被安全策略拒绝。
  判据：`tests/test_product_gaps_ui_v166.py`（16 例，含 4 条节点接线：验证节点采纳/失败进修复、
  清场剔除交付事实、孤立临时文件保护）与 `tests/test_v163_super_agent.py` 交付断言更新；
  `tests/test_output_styles_v47.py` 的默认提示词哈希钉随有意扩充同步更新。
  改前 12 红；变异电池 13/13 实测红在指定判据上、非 no-op、按原字节还原并核对 sha256。
  相邻套件 93 passed；后端全量 **4089 passed, 28 skipped, 0 failed in 804.41s**
  （上一基线 4073 + 本批 16；首轮唯一红是默认提示词哈希钉，随有意扩充更新后复跑全绿）。

- **Claude「Doing tasks」段六条与无测试配置时的收尾空跑（#384–#386）**：
  继续回答「同一模型为什么比不过」：三方对比用的是同一个模型（Spark），差距只能出在
  harness。上游 2.1.271 的 `Doing tasks` 段取证（`grep -boF` 现场复核偏移 101028812 /
  101029148 / 101030000 / 101031196 / 101031816 / 197932215）显示有六条我们一条没写：
  网页改动要真跑、不为假设需求设计、不为不会发生的场景兜底、不留兼容垫片与半成品、
  收尾不能停在「接下来我会……」、报告要分清验证过的与推断的。
  装置实物还抓到一条更直接的原因：UI 对比那一轮我们的任务 10 分钟超时在最后一次
  `run_tests` 审批上（`runs/ui-ours-events.json` seq 160–161），交付正文为空；工作区里
  既没有 pytest 配置也没有 package.json 的 test 脚本，这次审批注定只会换回
  「未识别到测试命令」。
  1. **提示词**：`CODING_INSTRUCTIONS` 增「最少代码」（不为假设的未来需求设计、只在系统
     边界校验、不留半成品与向后兼容垫片）与「网页/前端真跑」（单测和类型检查只证明代码
     正确、不证明页面能用；没法在浏览器里点就在交付里明说没验过）；收尾条件增「最后一段
     若是计划就先做完」与「验证过的 vs 推断的」。输出样式语义不变：编程三条随
     `keep-coding-instructions`，收尾两条属于永久段。
  2. **校验预检**：`_test_command_detected` 复用 `tools.testing.detect_test_command`
     （与 `run_tests` 不传 command 时同源）；检测不到测试配置、且本轮从没跑通过测试时
     直接记 skipped，不判定审批也不执行；「本轮跑通过、之后把配置改坏」仍走执行并判失败。
  判据：`tests/test_doing_tasks_prompt_v167.py`（10 例：7 条提示词 + 3 条校验预检，
  含「有配置照旧走闸门」「配置被本轮改坏仍失败」两条反向守卫）；改前 8 红。
  变异电池 7/7 实测红在指定判据上、非 no-op、按原字节还原并核对 sha256。
  真实工作区新旧同跑（`E:/tmp/replay_v167.py`，工作区取 UI 对比装置的 `runs/ui-ours`，
  旧代码取 HEAD 版 graph.py）：旧 decide=1 / execute=1，新 decide=0 / execute=0，
  结论 skipped。相邻套件 105 passed；后端全量 **4099 passed, 28 skipped, 0 failed
  in 812.84s**（上一基线 4089 + 本批 10）。
  真模型复跑（:8066 载入新代码、同一提示词、干净工作区，任务 a5e7dda8）：succeeded，
  18.6 分钟；5 次审批全是 `run_shell`，没有再出现注定空跑的 `run_tests` 审批；
  交付正文 281 字，校验采纳自跑脚本（`node .blank\scratchpad\selftest.js &&
  node .blank\scratchpad\servetest.js`，退出码 0），verifier 21 项判定全过；
  页面探针 22 步 0 失败。

- **空响应与零交付不许算成功（#394–#397）**：
  复杂题真模型复跑（:8066、Spark、任务 9eac5bed）出现一次「什么都没做也算成功」：
  seq23 `model.stream_fallback code=model_stream_empty`；seq24 非流式那次
  `completion_tokens=8192 / reasoning_tokens=8192`，正文与 tool_calls 皆空；旧 harness
  照常放行；seq35 验证节点对「零改动 + 交付自述没实现」判 `incomplete / passed=true /
  blocking=false`；seq36 `task.completed status=succeeded`，交付物 0 个文件。
  四处各自独立，逐条补：
  1. **空响应闸门**：`ask()` 在正文与工具调用都为空时记 `model.empty_response`，
     并带一条系统提示重试一次（`model.empty_response.retry`，提示里点明「你上一条回复
     是空的」且不替换原对话）；连续两次空抛 `ModelError("model_empty_response")`，
     API 返回 502、任务 `failed`，不再往下走去 summarize。
  2. **未完成词表**：`_INCOMPLETE_MARKS` 增「未实现 / 仍为空 / 没有任何改动 /
     未做任何改动」，交付正文自认没做就不得算完成。
  3. **零交付判据**：`verifier` 增 `delivery_matches_plan`——目标给了验收标准、计划里
     确实有 `edit`/`repair` 步、磁盘却零改动，判 blocking / failed；没有验收标准、
     没有编辑计划、或只读调研仍按原口径（纯问答不受影响）。
  4. **退路模型覆盖**：`_call_adapter` 的两条退路（流式报错 / 传输失败）此前丢掉了
     `model` 覆盖，退路等于换一个模型再问一遍；现在都带上。
  判据：`tests/test_empty_response_zero_delivery.py`（7 例，含「重试一次就恢复」「纯问答
  零改动不算失败」「有计划无验收标准仍是 incomplete」「只读调研 not_applicable」四条
  反向守卫）与 `tests/test_stream_output.py::test_fallback_keeps_the_model_override`
  （两条退路各断言一次）；改前目标全红。变异电池 8/8 实测红在指定判据上、非 no-op、
  按原字节还原并核对 sha256（`tests/mutation_battery_empty_response_v395.py`）。
  电池顺带抓到一条写宽的判据：重试提示的断言原本只查单字「空」，而系统提示本来就有
  这个字，改为「role=system + 专指短语」后 B 才如期红。相邻套件 148 passed；后端全量
  **4113 passed, 28 skipped, 0 failed in 825.68s**（上一基线 4105 + 本批 8）。

- **交付改动排除 scratchpad 自测脚本（#388）**：
  同一轮真模型复跑的交付正文写「改动：5 个文件（index.html、styles.css、app.js、
  .blank/scratchpad/selftest.js、.blank/scratchpad/servetest.js）、5 处 Diff」——提示词
  明确说 scratchpad 不属于交付物，模型照做了，交付却把两个自测脚本算成改动：用户数
  文件会多出两个没要的东西，Diff 计数也被脚本的反复改写撑大。
  `_change_facts` 增 `_in_scratchpad`：归一化反斜杠与 `./` 前缀后按目录排除
  `.blank/scratchpad/`，不按文件名猜——根目录下用户点名的 `_selftest.js` 仍算交付物，
  `.blank/` 下别的真改动（如 output-styles）也照算；只有 scratchpad 改动时不硬拼
  「改动：0 个文件」，退回原文。
  判据：`tests/test_delivery_scratchpad_v168.py`（6 例，含「根目录脚本仍算」「.blank
  其它目录仍算」两条反向守卫）；改前 4 红。变异 6/6 实测红在指定判据上、非 no-op、
  按原字节还原并核对 sha256。相邻套件 81 passed；后端全量 **4105 passed, 28 skipped,
  0 failed in 794.47s**（上一基线 4099 + 本批 6）。

- **产品间同提示词对比：评测器 `--plain-prompt`（#377）**：
  UI 交付对比要求三方收到同一段原文：`run.mjs` 新增开关，把 goal 拼成
  `goal + 验收标准 + 文件边界`，不再单发 `acceptance_criteria` / `goal_spec`，
  与 CLI 产品侧对比脚本的 `buildPrompt` 逐字同源；判分口径不放宽
  （仍按 `testCommand` + `expectedFiles` 硬比对）。

- **跑分暴露的三处产品缺陷：测试通道解释器解析 / 命令不存在归类 / 收尾交接收窄（#376）**：
  55 题基准在 DeepSeek 上丢的三题（coding-012/028/035）不是模型能力问题，是产品缺陷链：
  `run_tests` 传了自定义 `command` 时裸 `python` 原样交给 cmd，落到 PATH 上的商店占位符
  （退出码 9009），被记成「测试未通过」→ 模型据此认定环境没有解释器，下载便携运行时
  进工作区 → 清理被执行域不可逆闸门拦下 → 交付写「需要你：手动删除验证残留」→
  Verifier 把「需要你：」一律当成没做完。
  1. **解释器解析**：`normalize_python_command` 增加 `fallback` 参数；`run_tests` 的
     override 分支与不传 command 的那一路同源解析（venv → 本进程解释器；容器档用镜像
     自带的 `python3`）。`run_shell` 不传 fallback，行为保持不变（没有 venv 时仍原样
     执行，不猜）。
  2. **命令不存在归类**：退出码 9009/127 或回执命中「不是内部或外部命令」/「is not
     recognized …」时，`run_tests` 回 `status=error` + `command_not_found=true`，回执
     点名首个命令词并说明「这不是测试未通过」，给出改法（不传 command 走默认识别，
     或写解释器绝对路径），并明说不要为了跑测试在工作区下载运行时。
  3. **收尾交接收窄**：`_HANDOFF_MARKS` 只保留「让用户重做/恢复/修复工作」的四类词
     （恢复/重做/重新落/修复），移除「手工/手动/自行修复/自己修」——清理验证残留、
     补装工具链是收尾交接，不是交付物没做完；而这些残留的删除被执行域闸门拦下本身
     就是闸门的正确行为。
  判据：`tests/test_run_tests_detect_v152.py`（venv 优先 / 无 venv 回落本进程解释器 /
  容器档 `python3` / 三种显式 runner 不改写 / 9009 归类）、
  `tests/test_autorepair_verdict_v112.py`（012 与 028 两条真机交付原文不 blocking、
  反向守卫：要求用户恢复/修复/重做的交接仍 blocking）。改前旧代码上实测 6 红，
  其余为不变量守卫（改前即绿，防反向改动）。变异电池 6/6 实测红在指定判据上、
  非 no-op、按原字节（含 CRLF/LF 差异）还原并核对 sha256。相邻套件 82 passed；
  后端全量 **4073 passed, 28 skipped, 0 failed in 863.08s**（基线 4063 + 本批 10）。
  真实执行探针：修复后 coding-012 工作区里 `python -m pytest -q` 解析到工作区
  `.venv` 解释器，退出码 0（1 passed）。

- **任务级工作树的基准引用（`worktree.baseRef`，#323）**：此前 `enter_worktree` 一律从本地
  HEAD 建树，上游的设置项 `worktree.baseRef` 在本机没有对应物（README 一直列为偏离）。
  上游锚点（均以 `grep -boF` 现场复核）：设置面板的 `worktreeBaseRef` 选项
  `["fresh","head"]`（35550204）；设置键白名单（22632867）；工具描述
  `fresh` 默认从 `origin/<default-branch>`、`head` 从当前本地 HEAD（7059636）；
  建树消费点 `h = r?.fromHead ?? (!r?.prNumber && Ge().worktree?.baseRef==="head")`
  （26601992），非 head 且无 PR 时走远端基准（26605082），命令
  `git worktree add --no-track -B <branch> <path> <base>`；默认分支解析
  `git symbolic-ref --short refs/remotes/origin/HEAD` → 去 `origin/` 前缀并验证，
  否则候选 `main` / `master` 逐个验证，都不行返回 `main`（27311697）。
  1. **设置归一**：`normalize` 同时收 `base_ref` 与上游驼峰 `baseRef`（下划线优先），
     裁空白、转小写；认不出的值回落 `fresh`。`worktree` 段进 `DEFAULTS`，因此项目层
     / 本机层 / 用户层 / 管理层都能设置，且 `settings.py` 的既有层级合并直接生效。
  2. **服务层判定**：`worktree_session.enter(..., base_ref=)` 对非 `fresh`/`head` 的值
     归一成 `fresh`——这是公开入口，工具层已归一也不把它当 `head` 用。
  3. **`fresh` 路径**：默认分支按上游三段式解析；只读本机已有的
     `refs/remotes/origin/<branch>`，命中则基准为该远端提交；没有远端跟踪分支回落
     本地 HEAD（`base_ref` 记 `HEAD`）。
  4. **`head` 路径**：与旧行为一致，从当前本地 HEAD 建树。
  5. **可观察面**：`enter_worktree` 回执与 `worktree.entered` 事件都带
     `base_ref`（`origin/<branch>` 或 `HEAD`），回执文案写明基准；工具描述说明设置项。
  偏离项：（a）上游在 `FETCH_HEAD` 超过 24h 时发起 `git fetch`（5s 超时，失败告警并
  回落本地 HEAD）；本机**不在建树路径上发起 fetch**——网络出口统一归沙箱策略管，
  不在这条路径上偷偷开口子，只读已有远端跟踪分支；（b）建分支用 `-b` 而不是上游的
  `-B`：同名分支已存在时拒绝并提示换名字，而不是强制重置那棵树的分支。
  判据 `services/api/tests/test_worktree_base_ref_v323.py`（10 例：默认值与两种写法、
  项目层段不被拒、fresh 走 origin、head 走本地 HEAD、无远端回落、默认分支跟
  `origin/HEAD`、回落 `origin/master`、坏值按 fresh、服务层直调坏值也按 fresh、
  工具描述点名设置），变异 14/14 红（默认值改 head、只认下划线、删 `origin/HEAD`
  解析、候选表去掉 master、fresh 路径置空、无远端时返回空基准、工具恒返 fresh、
  工具恒返 head、工具不传 base_ref、工具描述改名、`tool_data` 丢 `base_ref`、
  服务层去掉归一、项目层拒收 `worktree`、session 的 `base_ref` 写死 HEAD；每条实测
  红在指定判据上，非 no-op，跑完按原字节还原并核对 sha256）。相邻套件（worktree 三套
  + settings 四套 + 本批）**167 passed in 28.91s**；后端全量
  **4063 passed, 28 skipped, 0 failed in 809.90s**（基线 4053 + 本批 10）。

- **子代理工具循环接入四类工具钩子（#320）**：#316 让定义里的 `hooks` 在子代理内注册，
  但子代理工具循环此前直接调用 `gateway.execute`，定义里写的 PreToolUse 守卫在子代理里
  静默不生效（#316 文档化偏离 c）。现在循环与父会话同一套顺序：pre → 执行/拒绝 → post
  → 批末。上游锚点（均以 `grep -boF` 现场复核）：base hook input 的 `agent_id` 描述
  `Subagent identifier. Present only when the hook fires from within a subagent (e.g., a tool called by an AgentTool worker). Absent for the main thread...`（24336471），
  并带 `agent_type`；`PreToolUse` 载荷 `{hook_event_name, tool_name, tool_input, tool_use_id}`
  （24337589）；`PostToolUse` 另带 `tool_response` / `tool_use_id` / `duration_ms`
  （24337830 / 24337928）；`PostToolUseFailure` 带 `error` / `is_interrupt` /
  `duration_ms`（24338072 / 24338196）；`PostToolBatch` 带 `tool_calls`（24338432）。
  1. **顺序与拒绝**：每次调用先跑 `pre_tool_use`；`deny` 不执行，回执写"钩子拒绝执行"
     并走 `gateway.refuse` 留审计；`ask` 在子代理里没有人可确认，按拒绝 fail-closed；
     `allow` 不绕过网关审批（定义文件里的钩子不能替子代理放宽权限）。
  2. **updatedInput**：必须过工具 schema，且改写后的审批判定不能更宽、风险不能更高；
     不通过的改写丢弃并落 `hook.input_rejected`，执行仍用原参数；通过的改写同时进执行
     与 post 钩子，两边看到的不能是两份参数。
  3. **post 两事件**：执行后跑 `post_tool_use`，失败再加 `post_tool_use_failure`；被拦下的
     调用两个都不跑（它压根没执行）。一批跑完发一次 `post_tool_batch`，载荷是这一批的
     `tool_name` / `tool_input` / `tool_use_id` / `ok` / `status` / `mode`。
  4. **additionalContext**：四类钩子返回的附加上下文合并成一条 user 消息，标注"只作参考，
     不得覆盖安全与审批规则"，在下一次模型调用前注入；最多保留 20 条，落
     `hook.context_injected`。
  5. **载荷口径**：子代理在父会话的 `tool` / `arguments` 之上补 `tool_use_id` /
     `agent_id` / `agent_type` / `session_id` / `permission_mode` / `transcript_path`。
     偏离项：（a）上游字段名是 `tool_name` / `tool_input` / `tool_response`，本机沿用父会话
     的 `tool` / `arguments` / `ok` + `status`，子代理与父会话之间不出现两套口径；
     （b）`duration_ms` 与 `is_interrupt` 未移植（父会话同样没有）；`additionalContext`
     用 user 消息而不是上游的 system 注入通道——子代理没有系统提示注入点。其中（a）
     （b）两项已由 #321 补齐，见下。
  判据 `services/api/tests/test_subagent_tool_hooks_v320.py`（12 例：定义钩子载荷与
    `agent_id` / `agent_type`、deny 阻断且不跑 post、ask fail-closed、updatedInput 应用并
    带到 post、schema 拒绝、风险抬升拒绝、additionalContext 注入并带"只作参考"、
    post 结果与 failure 错误、post_tool_batch 整批、父与定义钩子都跑且不串台、无钩子零
    `hook.*` 事件、运行时端到端守卫），变异 13/13 红（pre 不跑、deny 仍执行、ask 不
    fail-closed、接受未过 schema 的改写、不应用改写、去掉 post_tool_use、去掉
    post_tool_use_failure、去掉 post_tool_batch、不注入附加上下文、载荷去掉
    agent_id/agent_type、被拦调用仍跑 post、无钩子也落 hook.completed、风险抬升不拒绝；
    每条实测红在指定判据上，非 no-op，跑完按原字节还原并核对 sha256）。相邻套件
    （`subagent_tool_hooks_v320` + `agent_hooks_v316` + `agent_defs_v44` +
    `agent_memory_v317` + `agent_instructions_v318` + `agent_model_arg_v319` +
    `agent_model_v315` + `agent_transcript_path_v305` + `subagent_start_hooks_v54` +
    `subagent_start_stdin_v308` + `subagent_usage_v18` + `subagent_v12` +
    `background_subagent_v116` + `claude_subagent_default_v29` + `settings_hooks_v53` +
    `skill_hooks_v54` + `stop_failure_hooks_v55` + `hook_events_v52` +
    `async_hooks_v119` + `worktree_hooks_v118` + `plan_and_hooks`）**321 passed in 58.49s**；
    后端全量 **4040 passed, 28 skipped, 0 failed in 814.88s**（基线 4028 + 本批 12）。

- **工具钩子载荷字段对齐上游（#321）**：此前父会话与子代理给钩子的都是 `tool` /
  `arguments`，照着 Claude 文档读 `tool_name` 的钩子拿到 `undefined`；post 也没有
  `tool_response` / `duration_ms`，失败事件没有 `is_interrupt`。上游锚点（均以
  `grep -boF` 现场复核）：`PreToolUse` 载荷 `{hook_event_name, tool_name, tool_input,
  tool_use_id}`（24337589）；`PostToolUse` 带 `tool_name` / `tool_input` /
  `tool_response` / `tool_use_id` / `duration_ms`（24337830 / 24337928），`duration_ms`
  的描述是 `Tool execution time in milliseconds. Excludes permission-prompt and hook
  time.`（24337965）；`PostToolUseFailure` 带 `tool_name` / `tool_input` /
  `tool_use_id` / `error` / `is_interrupt` / `duration_ms`（24338072 / 24338233）；
  `PostToolBatch.tool_calls` 每项 `{tool_name, tool_input, tool_use_id,
  tool_response?}`（24338196 / 24338432）。
  1. **两套名字一起下发**：父会话 `_tool_hook_fields` 与子代理 `_tool_hook_payload`
     同时给 `tool` / `arguments` 与 `tool_name` / `tool_input` / `tool_use_id`；
     旧钩子不静默失效。
  2. **post / failure**：`tool_response` 取网关回执投影 `{ok, status, output, data}`
     （`output` 截断 4000），不含 `tool_call_id` / `permission` / `sandbox_backend`
     这类账本与审批内部字段——钩子是观察者，不该顺走治理数据；`duration_ms` 直接
     复用网关回执的执行耗时（审批与钩子本身都在它之外）。
  3. **失败事件**：`is_interrupt` 恒为 `false`——本机没有"用户中断工具"这一维度。
  4. **batch**：`tool_calls` 每项补 `tool_response`，与 post 同一投影。
  判据 `services/api/tests/test_tool_hook_payload_v321.py`（4 例：父会话成功路
    pre/post/batch 的上游字段与旧字段共存、`tool_response` 投影不含账本字段；父会话
    失败路 `error` / `is_interrupt` / `duration_ms`；子代理成功路 pre/post/batch；
    子代理失败路），变异 12/12 红（父/子各丢上游名、丢历史名、`tool_response` 丢 ok、
    post 丢 duration_ms、failure 丢 is_interrupt、batch 丢 tool_response、投影直接
    透传原回执；每条实测红在指定判据上，非 no-op，跑完按原字节还原并核对 sha256）。
    相邻套件（`tool_hook_payload_v321` + `subagent_tool_hooks_v320` +
    `agent_hooks_v316` + `hook_events_v52`）**60 passed in 22.62s**；后端全量
    **4044 passed, 28 skipped, 0 failed in 823.28s**（基线 4040 + 本批 4）。

- **子代理模型强制（`CLAUDE_CODE_SUBAGENT_MODEL_FORCE`，#322）**：上游在环境变量置位
  时让调用方与定义里的 `model` 一起失效；本机此前没有这条链路，README 一直把它列为
  偏离。上游锚点（均以 `grep -boF` 现场复核）：Agent 工具 schema 直接剃掉 model
  （`return a.CLAUDE_CODE_SUBAGENT_MODEL_FORCE?n.omit({model:!0}):n`，4302142）；
  解析入口先置空（`re(n,e){if(a.CLAUDE_CODE_SUBAGENT_MODEL_FORCE)n=void 0;...}`，
  36500302；`H(n,e,o="tool"){if(...)n=void 0;...}`，36500731）；Workflow agent 忽略
  并告警（4582183）；changelog 口径是「apply `CLAUDE_CODE_SUBAGENT_MODEL` (or the
  main model) to every subagent」（20210432）。
  1. **判定**：上游按字符串真值——非空即开；本机照此，不额外接受 `0` / `false` 关闭。
  2. **schema**：置位时 `spawn_agent` 不再暴露 `model`；`spawn_agents` 的任务文档改成
     「model 在本会话被忽略，不要传」。
  3. **生效值**：工具入参与定义里的 `model` 都失效；本机没有
     `CLAUDE_CODE_SUBAGENT_MODEL` 的对等物，所以一律继承父会话模型
     （`request.model = ""`）。批量子任务与 Skill/命令的 fork 派发都走
     `_apply_definition`，同一处生效。
  4. **留痕**：调用方仍塞 model 时，前台与后台回执都带 `model_ignored` 与一句
     「已设置…忽略传入的 model」；非字符串不再 fail-closed——这个值不会被使用。
  5. **续聊**：第一轮的空覆盖原样带到下一轮。
  判据 `services/api/tests/test_subagent_model_force_v322.py`（9 例：置位剃 schema、
  未置位保留（两向）、批量文档改写、前台忽略并留痕、定义 model 被剥离、非字符串
  不拒绝、后台忽略并留痕、批量逐项剥离、续聊沿用空覆盖），变异 12/12 红（force 恒关 /
  恒开、schema 不剃、批量文档不变、定义 model 不失效、工具 model 不失效、不留痕、
  前台丢 `model_ignored`、前台文案不写、非字符串仍拒绝、后台文案不写、后台丢
  `model_ignored`；每条实测红在指定判据上，非 no-op，跑完按原字节还原并核对
  sha256）。相邻套件（`subagent_model_force_v322` + 23 个 agent/钩子/子代理套件）
  **349 passed in 61.32s**；后端全量 **4053 passed, 28 skipped, 0 failed in 855.75s**
  （基线 4044 + 本批 9）。Workflow
  agent 的 `agent()` 选项本机没有 `model` 键，4582183 那条告警分支在本机是空集，
  未造替身。

- **子代理派发的 `model` 入参覆盖（#319）**：#315 只移植了定义里的 `model`，模型自己
  调用 `spawn_agent` 时无法按任务挑模型；现在工具入参 `model` 压过定义值。上游锚点
  （均以 `grep -boF` 现场复核）：Agent 工具 schema 的 `subagent_type`
  （4299449）与 `model` 描述
  `Optional model override for this agent. Takes precedence over the agent definition's model frontmatter and the configured default subagent model. If omitted, uses the agent definition's model, else the default (inherits from the parent unless a default subagent model is configured).`（4299605），
  同句
  `Ignored for subagent_type: "fork" — forks always inherit the parent model.`（4299889）；
  运行期把工具模型带进派发、fork 形态直接丢弃（`model:Jt?void 0:we`，4311121；
  插件 spawn 路径同口径 4311566），队友路径的 `modelSource:pe?"tool":"frontmatter"`
  （4307321）确认「工具值优先于定义值」。
  1. **优先级**：显式 `model` 压过定义 frontmatter；不传、`null` 时仍用定义；显式空串
     或 `inherit` 是「回落父会话」的语义值，会压掉定义值；空白裁掉；非字符串
     fail-closed（把 `123` 当模型名发给供应商只会换来 400），不会派出子代理。
  2. **fork**：入参 `model` 被忽略，分身恒继承父会话模型（事件里 `agent_model` 仍记空）。
  3. **批量与续聊**：`spawn_agents` 每个子任务各自携带 `model`，未写的沿用定义；
     `_remember` / `send_message` 让续聊沿用第一轮的生效值，不会漂回定义值。
  4. **偏离项**：（a）上游 `sonnet` / `opus` / `haiku` / `fable` 四别名枚举没有强制——
     本机供应商模型名不同，任意字符串原样透传（与 #315 定义口径一致，写错由供应商报）；
     （b）`CLAUDE_CODE_SUBAGENT_MODEL_FORCE` / 协作模式下强制 worker 继承的链路未移植，
     本机没有对等环境变量。
  判据 `services/api/tests/test_agent_model_arg_v319.py`（12 例：schema 两例；工具入参
    压过定义、不传保留定义、空串回落、`inherit` 保留、非字符串 fail-closed 五例；
    fork 忽略一例；批量每任务携带、批量非字符串拒绝、续聊沿用三例；端到端一例——mock
    供应商在子代理那次请求里看到 `mock-tool-model`、且不再发定义里的 `mock-fast`），
    变异 10/10 红（忽略工具入参、fork 不排除、非字符串原样接受、缺失当成空串覆盖、
    批量忽略 per-task model、批量不校验、schema 缺 model、批量文档缺 model、空白不裁、
    空串当缺失；每条实测红在指定判据上，非 no-op，跑完按原字节还原并核对 sha256）。
    相邻套件（`agent_model_arg_v319` + `agent_model_v315` + `agent_defs_v44` +
    `claude_fork_v30` + `claude_subagent_default_v29` + `claude_send_message_v30` +
    `background_subagent_v116` + `parallel_budget_v26` + `worktree_parallel_v13` +
    `subagent_v12`）**165 passed in 22.94s**；后端全量 **4028 passed, 28 skipped,
    0 failed in 842.76s**（基线 4016 + 本批 12）。

- **子代理默认继承 AGENTS.md 指令，`omitClaudeMd` 可关闭（#318）**：此前子代理系统提示
  只有基础规则与定义正文，项目/用户 AGENTS.md 一个都不带；现在默认继承，定义里写
  `omitClaudeMd: true` 时关闭。上游锚点（均以 `grep -boF` 现场复核）：agent schema 键
  `omitClaudeMd`（6478360）与描述
  `If true, the agent runs without the user, project and local CLAUDE.md instruction files when spawned as a subagent; managed policy files are kept.`（6478399）；
  同块 `initialPrompt` 明确
  `Auto-submitted first message when this agent runs as the main session (via --agent or settings). Not read when spawned as a subagent.`（6478009），
  故不用于子代理、本批不做；文件解析
  `pe=r.omitClaudeMd,Se=pe==="true"||pe===!0?!0:void 0`（20542883）；派发闸门
  `e.omitClaudeMd&&!Se?.userContext?await wDs(e,Ko,r):{userContext:Ko,managedInstructionsOnly:!1}`
  （15186251）；`wDs` 先摘掉 `claudeMd`，内置/策略来源直接返回，否则读托管策略内容、
  非空时放回并置 `managedInstructionsOnly:!0`（15205840）；内置 Explore（9239069）与
  Plan（11209824）写死 `omitClaudeMd:!0`；插件/JSON 路径原样携带（25141156）。
  1. **解析**：`AgentDefinition.omit_claude_md`；只认字面 `true`（布尔或字符串 `"true"`），
     其余值静默按未声明处理、不进 `ignored`（上游 `Se` 同口径）；`as_dict()` 只在声明时
     带 `omitClaudeMd`。
  2. **派发**：`run_subagent` 非 fork 默认 `load_layers(run_root)`，用 `compose` 拼进
     子代理系统提示（基础规则 → 指令层 → 记忆段 → 定义正文）；`omit_claude_md` 为真时
     指令层为零层，定义正文与基础规则照旧。成功注入落 `subagent.instructions`
     （`omitted:false` + `provenance(layers)`：source / 掩码路径 / chars / sha /
     truncated），omit 生效落 `omitted:true` + `layers:[]`；没有指令文件时不发事件；
     fork 不发事件、也不重复注入（父会话系统提示里已带）。
  3. **携带**：`delegate._apply_definition` 把定义带进 `SubAgentRequest.omit_claude_md`
     （`spawn_agent` / `spawn_agents` 同一路）；`_remember` 保存、`send_message` 恢复，
     续聊仍是同一个子代理，第二轮不会把第一轮关掉的指令悄悄读回来。
  4. **偏离项**：（a）指令文件是本地那套 `AGENTS.md` / `.blank/AGENTS.md` / harness /
     `BLANK_INSTRUCTIONS_FILE`，不是 CLAUDE.md；（b）本机没有托管策略级指令层，所以
     omit 生效时一个指令层都不注入，上游会保留托管策略内容；（c）fork 不重复注入——
     fork 的系统提示就是父会话原文，父提示里已经带着指令层；（d）本机内置子代理只有
     `general-purpose` / `fork`，没有上游 Explore / Plan 那两档，所以没有对应的
     `omitClaudeMd:!0` 写死项。
  判据 `services/api/tests/test_agent_instructions_v318.py`（16 例：解析五组含布尔/字符串
    true、四种非 true 静默 False、缺失、插件 agent；派发六例含项目/用户 AGENTS.md 注入与
    审计事件（层来源、掩码路径、字符数、JSON 不含工作区绝对路径）、omit 不注入但保留
    定义正文、无文件不发事件、fork 不注入且不发事件；携带两例批量派发与续聊；变异 11/11
    红（解析恒 False、非 true 也置位、派发忽略 omit、默认不注入、fork 也注入、事件整块
    消失、空层也发事件、路径未掩码、delegate 不携带、续聊丢 omit、续聊不保存），每条
    实测红在指定判据上，跑完按原字节还原并核对 sha256）。相邻套件（`agent_instructions_v318`
    + `agent_memory_v317` + `agent_model_v315` + `agent_hooks_v316` + `agent_defs_v44` +
    `subagent_v12` + `subagent_start_hooks_v54` + `background_subagent_v116` +
    `claude_fork_v30`）**180 passed in 19.12s**；后端全量 **4016 passed, 28 skipped,
    0 failed in 835.80s**（基线 4000 + 本批 16）。

- **子代理定义的 `memory` 持久记忆（#317）**：agent frontmatter 的 `memory` 从「识别但
  不生效」变成真的给这个子代理一套跨会话记忆。上游锚点（均以 `grep -boF` 复核）：
  agent schema 的 `memory` 描述与枚举
  （`Scope for auto-loading agent memory files. 'user' - ~/.claude/agent-memory/<agentType>/, 'project' - .claude/agent-memory/<agentType>/, 'local' - .claude/agent-memory-local/<agentType>/`，24362146）、
  名字消毒 `nl`（23276603）、目录 `Zf`（23276684）与 `MVe`（23276905）、派发
  `oNt`（23277568，三条 scope 指导语 + `Pe(h,r)` 建目录 + `Pa`）、模板 `Pa`
  （22492834，先 `zo(...)` 再 push `## MEMORY.md` 段：有内容内联、为空写空态）、
  `gc="MEMORY.md"`（29014865）、插件 agent 的 memory 校验（`_kn` 5853486；非法值
  只告警 `has invalid memory value`，5854579）。
  1. **解析**：`AgentDefinition.memory`；`memory` 从 `UNSUPPORTED_FIELDS` 移除、不再
     默认进 `ignored`。字符串 trim + 小写后只认 `user` / `project` / `local`；非法值
     与非字符串只告警并进 `ignored`，不拒绝整份定义；插件 agent 同口径（上游
     `_kn.includes(dt)`，非法只告警）。`as_dict()` 只在声明时带 `memory`。
  2. **目录**：`sanitize_agent_name` 对齐 `nl`（`[^A-Za-z0-9\-_]` → `-`，空串 →
     `unknown`）；`project` → `<项目根>/.blank/agent-memory/<name>`、`local` →
     `<项目根>/.blank/agent-memory-local/<name>`、`user` →
     `<用户配置根>/agent-memory/<name>`（默认 `~/.blank/agent-memory`，
     `BLANK_USER_AGENT_MEMORY_DIR` 可改）。
  3. **提示段**：`render_memory_section` 按上游 `Pa` 的顺序拼
     `# Persistent Agent Memory` → 目录介绍 → 构建/记住遗忘 → 类型 → What NOT →
     How to save → When to access → Before recommending → 三条 scope 指导语（逐字）→
     `## MEMORY.md` + 内联内容或空态文案。读入的 `MEMORY.md` 超 12000 字时保留尾部
     并加一行截断标记（记忆是追加写的，最新在末尾）。
  4. **接线**：`_apply_definition` 把定义带进 `SubAgentRequest.memory_scope`（批量
     派发同一条路），`normalized()` 保留。`run_subagent` 在
     `assert_not_looser` 之后建目录（`fork` 形态不建），把提示段接在子代理系统提示
     末尾（`instructions` 之前），并往工具池追加 `memory_write`——目录钉死在闭包里、
     没有路径参数，所以子代理只可能写本作用域的 `MEMORY.md`；`disallowedTools`
     含 `*` 或 `memory_write` 时不追加（定义说了禁用就禁用）。建目录/读文件失败回
     `subagent.memory_failed`（scope + 错误），成功落 `subagent.memory_enabled`
     （scope / 打码目录 / 字符数）。续聊由 `_remember` / `send_message` 保留作用域。
  5. **偏离项**：（a）不自动给子代理补 Read/Write/Edit 工具——上游会给声明了
     `memory` 的 agent 补读写工具，本机子代理工具池不因定义扩大，改为只注入
     `memory_write` 单工具；（b）上游「一文件一记忆 + `MEMORY.md` 索引」的布局压缩成
     单一 `MEMORY.md` 条目流（与主代理项目长期记忆同一格式，300 字上限、同句去重、
     超限丢最旧）；（c）远程记忆目录（`CLAUDE_CODE_REMOTE_MEMORY_DIR`）与
     `primedEntrypoint` 未移植——本机没有远程记忆存储；（d）截断按字符数保留尾部，
     不移植上游按行/字节统计的 `was_line_truncated` / `was_byte_truncated` 标注。
  判据 `services/api/tests/test_agent_memory_v317.py`（28 例：解析八例含插件与非法值、
    目录三例含名字消毒与环境变量覆盖、提示段与追加六例含三条指导语逐字/空态/内联/截尾、
    接线十一例含真实派发建目录与注入、`memory_write` 落盘后下一轮读得到、去重、坏类型
    拒绝、未声明零副作用、`disallowedTools` 只挡工具不挡提示段、fork 不建目录、续聊保留、
    批量派发携带），变异 20/20 红（逐条改坏实现：名字空串不回落、project/local 目录
    互换、丢 scope 指导语、丢空态文案、读文件头而不是尾、关掉去重、关掉建目录、关掉
    超限裁剪、关掉类型校验、不加 `memory_write`、不注入提示段、关掉 fork 闸门、关掉
    `disallowedTools` 闸门、续聊保存/恢复各丢一次作用域、`delegate` 不携带作用域、
    非法值不告警、不发审计事件、事件里用未打码绝对路径；红在指定判据上，跑完按原字节
    还原并核对 sha256）。相邻套件（`agent_memory_v317` + `agent_model_v315` +
    `agent_hooks_v316` + `agent_defs_v44` + `subagent_v12` + `subagent_start_hooks_v54` +
    `background_subagent_v116`）**149 passed in 18.70s**；后端全量 **4000 passed,
    28 skipped, 0 failed in 834.17s**（基线 3972 + 本批 28）。

- **子代理定义的 `hooks` 作用域注册（#316）**：agent frontmatter 的 `hooks` 从「识别但
  不生效」变成真的在这一轮子代理里跑，且只影响这个子代理、不改父会话。上游锚点（均以
  `grep -bo` 复核）：派发函数在 SubagentStart 迭代**之后**才注册定义钩子
  （`let ia=!oc("hooks")||hV(e.source);if(SQt(e.hooks)&&ia)...hkr(...)`，15189617）、
  注册函数 `hkr` 把 `Stop` 改写为 `SubagentStop` 并记
  `Converting Stop hook to SubagentStop for ... (subagents trigger SubagentStop)`
  （15178981）、解析函数 `Z5` / `xt` 的守卫口径（17401050，判定理由字符串
  `a PreToolUse/PermissionRequest hook that cannot be loaded may be what guards the
  permissions declared beside it, so nothing it sits in is applied until the entry is
  fixed or removed`，17400867）、插件 agent 的 `permissionMode` / `hooks` /
  `mcpServers` 一律忽略（`Skn`，5853669 / 5854869）。
  1. **解析**：`AgentDefinition.hooks`（事件 → `HookSpec` 元组）。收 Claude 的嵌套形状
     （数组项含 `matcher` + `hooks` 数组），PascalCase 事件名折成内部蛇形名；`Stop` 在
     解析层原样保留（改写发生在注册期）。守卫事件 `pre_tool_use` / `permission_request`
     的坏条目（事件值不是数组、条目非对象、缺 `type`、未知 `type`、schema 不过）→
     **整份 Agent 不加载**并点名 `hooks.<event>`；非守卫事件的坏条目只丢弃并告警；未知
     事件丢弃并告警；`hooks` 不是对象同样不加载。`hooks` 从 `UNSUPPORTED_FIELDS` 移除、
     不再进 `ignored`；插件 agent 的 `hooks` 仍整段忽略。`as_dict()` 只报每个事件几条
     （命令正文里可能有 token/路径，审计不回显）。
  2. **组装**：`SubAgentRequest.agent_hooks` 由 `_apply_definition` 从定义带进请求
     （`spawn_agent` 与 `spawn_agents` 两条路都走这里），`normalized()` 保留。
  3. **运行期**：`run_subagent` 在 SubagentStart 钩子跑完**之后**调 `_scope_agent_hooks`：
     新建一份合并映射（父事件 + 定义事件，按「事件 + 命令 + matcher + 超时」去重），
     `Stop` 注册成 `subagent_stop`；父 runner 的映射不被改写；新 runner 复制
     `settle_timeout` / `warnings` / `prompt_runner` / `mcp_runner`，并共享
     `_tasks` / `_completed` / `_wakeups` 异步队列；`disableAllHooks` 下直接沿用父
     runner、不复活定义钩子；定义钩子看不到自己的 SubagentStart（start 仍走父 runner）；
     注册结果落 `subagent.hooks_registered`（`events` / `sources` / `converted`）。
     stop gate、完成通知、worktree 清理三处改走 `child_hooks`。
  4. **续聊**：`_remember` 保存、`send_message` 恢复 `agent_hooks`——第二轮仍按同一份
     定义钩子跑。
  5. **偏离项**：（a）本机没有工作区信任对话框，工作区/用户来源的定义一律视为可信；
     上游对不可信来源会跳过 frontmatter hooks（`sze`，5853669 同一行）；（b）未知事件键
     里若藏着守卫内容（如拼错成 `PreToolUSe`）上游整份拒绝该 Agent，本机只按未知事件
     丢弃并告警；（c）子代理工具循环不执行 PreToolUse / PostToolUse（父会话的这两类
     钩子在子代理里同样不跑，是既有缺口），因此定义里写它们只在子代理内静默不生效，
     可观测的是 `permission_request` / `subagent_stop` / `notification` /
     `worktree_remove` 这些走真实管线的事件。
  判据 `services/api/tests/test_agent_hooks_v316.py`（20 例：解析九例含守卫整份拒绝、
    非守卫丢弃、未知事件、插件忽略、`as_dict` 不泄漏命令；组装三例含批量派发与
    续聊载荷；运行期七例含定义 `permission_request` 在真实工具循环里放行、`Stop` →
    `subagent_stop`、完成通知、worktree 清理、看不到自己的 SubagentStart、
    `disableAllHooks`、续聊两次都跑；端到端一例走真实 HTTP 任务派发文件型子代理），
  变异 14/14 红（逐条改坏实现：解析开关关掉、守卫坏条目只丢弃、不查顶层守卫键、
  `Stop` 不改写、注册进父 runner、无视 `disableAllHooks`、先注册再跑 Start、
  `as_dict` 泄漏命令、插件 agent 也解析、不发注册事件、续聊丢定义钩子、批量派发不带
  钩子、通知回到父 runner、副本清理回到父 runner；红在指定判据上，跑完按原字节还原并
  核对 sha256）。相邻四十二套件 686 passed（153.18s，含 agent/subagent/hook/skill/
  worktree/claude/team/background 各家族）；后端全量 **3972 passed / 28 skipped /
  0 failed**（831.32s，退出码 0），较 #315 基线的 3952 恰为本批新增 20 例。

- **子代理定义的 `model` 覆盖（#315）**：agent frontmatter 的 `model` 从「识别但不
  生效」变成真的改这一轮子代理用的模型。上游锚点（均以 `grep -bo` 复核）：agent
  schema 的 `model` 描述（`Model override for this agent. Use \`inherit\` to match
  the spawning conversation.`，6477141）、Agent 工具入参的 `model` 四别名枚举
  `["sonnet","opus","haiku","fable"]`（4299551，描述写明「Takes precedence over the
  agent definition's model frontmatter and the configured default subagent model」）、
  派发初值 `pe=e.model` 与协调者强制继承的剥离点（4303537）、fork 的
  `let uo=Jt?"inherit":we`（4313268，`Jt` 即 fork）、解析函数 `TD(e,n,r,s,d)`
  （11899161：显式 `r` 优先，其次定义 `e`，`"inherit"` 直接回落父模型 `h()`，
  最后才看 `CLAUDE_CODE_SUBAGENT_MODEL`）。
  1. **解析**：`AgentDefinition` 新增 `model`（str，默认空串）。字符串原样保存
     （含 `inherit` 这个语义值），首尾空白裁掉；非字符串告警并丢弃，`null` /
     缺省静默；`as_dict()` 仅在非空时写出。`model` 从 `UNSUPPORTED_FIELDS` 移除，
     不再进 `ignored` 审计；插件 agent 照常生效（上游 `Skn` 只忽略
     `permissionMode` / `hooks` / `mcpServers`）。
  2. **组装**：`SubAgentRequest.model` 由 `_apply_definition` 从定义带进请求
     （`spawn_agent` 与 `spawn_agents` 两条路都走这里），`normalized()` 裁剪空白。
  3. **调用**：`run_subagent` 一轮只裁决一次 `child_model`——
     `inherit` / 空串 / fork 都换算成空覆盖（= 供应商配置的模型），字面量永远
     不发给供应商；该值同时喂给 effort 裁决（按真实模型封顶）和每一步的
     `bind_tools(..., model=...)`。`subagent.started` 新增 `agent_model`
     （生效覆盖，空串表示继承父会话）。
  4. **续聊**：`_remember` 保存、`send_message` 恢复 `model`——第一轮用哪个模型，
     第二轮仍用哪个，不会同一段对话中途换脑子。
  5. **偏离项**：工具入参的 `model` 四别名枚举没有移植（本机供应商模型名不同，
     照搬要么放宽、要么误发 400；定义里的 `model` 原样透传）；
     `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`（2678990）剥离调用方显式模型的链路未移植
     ——本机没有这个环境变量的对等物。
  判据 `services/api/tests/test_agent_model_v315.py`（17 例：解析八例含插件来源、
    不支持清单关系、`inherit` 保留与空白裁剪、非字符串告警不进 ignored；
    组装三例含批量派发；调用四例含 inherit 回落、fork 忽略覆盖、续聊沿用、
    事件报生效值；端到端一例钉住 mock 供应商在子代理那次请求里看到的确实是
    定义值、父代理仍用自己的供应商模型），变异 11/11 红（解析整块拿掉 → 9 红；
    回到不支持清单 → 2 红；`as_dict` 不写 → 2 红；组装不携带 → 4 红；
    `normalized` 丢字段 → 5 红；fork 闸门拿掉 → 1 红；inherit 字面量下发 → 1 红；
    `bind_tools` 不传 model → 3 红；续聊两处各摘一条 → 各 1 红；事件改记声明值
    → 1 红；红集合逐条实测、还原后字节一致），相邻十七套件 313 passed
    （`test_agent_defs_v44`、`test_agent_model_v315`、`test_subagent_v12`、
    `test_subagent_usage_v18`、`test_agent_continuation_v25`、`test_claude_fork_v30`、
    `test_claude_subagent_default_v29`、`test_background_subagent_v116`、
    `test_agent_e2e_v13`、`test_agent_boundary_m3_v46`、`test_effort_batch`、
    `test_skill_agent_background_v303`、`test_commands_v46`、`test_skill_model_v52`、
    `test_skill_fallback_v314`、`test_subagent_start_hooks_v54`、
    `test_subagent_start_stdin_v308`；其中 v44+v315 79 例），后端全量
    **3952 passed / 28 skipped / 0 failed**（832.01s，退出码 0）。同步修正 v44
    里钉住旧差集 `["model"]` 的判据，改为钉 `agent_ignored == []` 与事件里的
    `agent_model`。

- **Skill 的 `fallback` 薄指针让位（#314）**：Skill 声明 `fallback: true` 时，一旦
  加载了**同后缀**的插件技能（`<plugin>:<name>`），这份占位就丢弃并留告警。
  上游锚点（均以 `grep -bo` 复核）：Skill schema 的 `fallback` 描述（`@internal —
  interim defense-in-depth for thin-pointer skill stubs. If true, this skill yields
  to a same-suffix plugin or MCP skill ...`，6475337）、合并层过滤函数 `q9(e)`
  （12713520：先收集 `loadedFrom ∈ {plugin,bundled,mcp}`、非
  `disableModelInvocation`、非 `Rj` 的 prompt 命令名的最后一段
  （`lastIndexOf(":") > 0`），再丢弃 `v2e` 为真且名字命中的条目并告警
  `Dropping fallback skill '<name>' — ...`，12713951）、`v2e(e)`
  （12715657：`type==="prompt" && fallback===!0 && loadedFrom!=="syncedSkills"`）。
  1. **解析**：`SkillDefinition` 新增 `fallback`（bool，默认 False）。`_definition_from_dir`
     沿用 `_flag` 口径：`true` / `false` 与字符串 `"true"` / `"yes"` / `"1"` / `"on"`
     （及反值）都认，其他值告警并按 False 处理，`null` / 缺省静默。`as_entry()`
     恒携带（合并层要读它），`as_dict()` 仅在 True 时写出。`SKILL_UNSUPPORTED_FIELDS`
     排除 `fallback`（`UNSUPPORTED_FIELDS` 全局保留）；命令侧上游 schema 无此字段，
     `COMMAND_UNSUPPORTED_FIELDS` 仍包含它。
  2. **合并**：`merge_skill_entries(*groups, dropped=None)` 在既有去重之后做一次
     后缀过滤——收集「`source` 以 `plugin:` 开头 + 可模型调用 + 名字带冒号」的
     最后一段为集合；`fallback is True` 且名字命中该集合的条目丢弃，命中的名字
     追加进 `dropped`。runtime 两处合并点（`receive_task` 与 SessionStart 重载）
     收集 `dropped` 并落 `skills.fallback_shadowed` 事件（重载那份带
     `event: "session_start"`），让「薄指针被顶掉」进证据时间线而不是静默消失。
  3. **偏离项**：本机没有 bundled / MCP prompt 技能与 `syncedSkills` 链路，
     后缀集合只看 `plugin:` 来源；`_flag` 对字符串真值的宽容与 `background`
     一致，上游 zod boolean 只收布尔。
  判据 `services/api/tests/test_skill_fallback_v314.py`（26 例：默认 false、true /
  false、字符串真值四档、非布尔告警、`null` 静默、`as_entry` / `as_dict` 携带、
  审计省略 false、三份清单关系、`ignored` 不含 fallback；合并层后缀命中丢弃 /
  未命中保留 / 非 fallback 不丢 / 不可调用插件项不触发 / 插件命令同样触发 /
  dropped 告警文本 / 无命中空 / 多 fallback 只丢命中 / 无插件层原样 / 目录清单
  隐藏 / 非插件来源带冒号不触发 / 无冒号不触发 / 端到端 two 例；运行时
  `receive_task` 与 reloadSkills 两条链路各一条真跑，钉住
  `skills.fallback_shadowed` 事件与 `dropped` 载荷），变异 13/13 红（解析恒 False
  → 6 红；非法值不告警 → 1 红；`as_entry` 不携带 → 4 红；留在不支持清单 → 2 红；
  合并从不丢弃 → 8 红；不可调用插件项计入 → 1 红；任意来源计入 → 1 红；忽略
  fallback 标志 → 2 红；dropped 不记录 → 3 红；两处接线各摘一条 → 各 1 红；
  两处事件名各改一处 → 各 1 红；红集合逐条实测、还原后 26 绿），相邻十九套件
  **512 passed**（同步修正 v303 里钉住旧差集的判据）。

- **Skill / 命令的 `shell` 字段（#313）**：`!` 命令块按 frontmatter 声明的解释器
  执行，而不是一律走 host shell。上游锚点（均以 `grep -bo` 复核）：
  Skill schema 的 `shell` 描述（`Shell for \`!\`-command blocks: \`bash\` or
  \`powershell\`. Defaults to bash regardless of platform.`，6474227）、解析函数
  `Osn(e,n)`（17187055：`R=["bash","powershell"]`，`String(e).trim().toLowerCase()`
  后精确匹配；`null`/空串直接返回 undefined；非法值告警
  `Frontmatter 'shell: ...' in ... is not recognized. Valid values: bash,
  powershell. Falling back to bash.` 并按未声明处理）、插件侧缺二进制的提示文案
  （4020401）。
  1. **解析**：`SkillDefinition` / `CommandDefinition` 新增 `shell`（str，默认空串）；
     `skills_defs._parse_shell` 由两侧共用，首尾空白与大小写归一后精确匹配
     `bash` / `powershell`；`null` / 缺省 / 空串静默按「未声明」；非法值与非字符串
     告警并丢弃（不猜一个解释器）。`shell` 从 `UNSUPPORTED_FIELDS` 移除，
     `SKILL_UNSUPPORTED_FIELDS` / `COMMAND_UNSUPPORTED_FIELDS` 随之不再包含它，
     也不再进 `ignored` 审计。
  2. **执行**：`prompt_args._resolve_shell_command` 把 `bash` 解析成
     `[bash, -c]`、`powershell` 解析成 `[pwsh|powershell, -NoProfile, -Command]`
     （`pwsh` 优先，与上游插件侧检查的两个名字一致）；`execute_shell_blocks` 用
     `shell=False` 的 argv 前缀运行 `!` 块；`render_prompt` → `render_skill` →
     命令展开 / skill 工具 / 子代理预加载五条路径全部透传 `shell`。
  3. **偏离项**：**未声明时沿用 host shell（Windows 上是 cmd），不是上游「默认
     bash、不分平台」**——`!` 块的既有契约（#309）就是 host shell，无声明即改 bash
     会悄悄改变存量命令文件的执行语义；显式 `shell: bash` / `shell: powershell`
     才切换。声明的二进制在 PATH 上找不到时同样静默回落 host shell（上游在插件
     侧告警，本机没有插件级可用性检查这条链路）。本字段只影响渲染时的 `!` 块，
     不改变 `run_shell` 工具的执行方式。
  判据 `services/api/tests/test_shell_field_v313.py`（22 例：默认空串、大小写/空白
  归一、非法值告警并丢弃、`null` 不告警、非字符串告警、`as_entry` 与审计携带、
  未声明不进审计、两份不支持清单排除；命令侧解析与非法值两例；真实 PowerShell
  语法 `<mark>` 跑通 + host shell 反证、真实 bash 算术 `$((6*7))` + host 反证、
  二进制缺失回落；`render_prompt` / `render_skill` / 命令展开 / 预加载 / 工具处理器
  五条透传路径与目录条目），变异 7/7 红（解析恒不返回候选值 → 6 红；非法值不
  告警 → 3 红；`shell` 留在不支持清单 → 3 红；执行忽略 `shell` 参数 → 7 红；
  `render_prompt` 不传 → 5 红；`render_skill` 不传 → 3 红；目录条目不携带 → 3 红；
  红集合逐条实测、互不相同，还原后 22 绿），相邻九套件连同本批 **200 passed**。

- **命令/Skill 的 `context: fork` 派发闸门（#312）**：修正 #311 的语义偏差——上游
  的派发闸门是 `context == "fork"`，`agent` 只是"fork 时用哪个子代理类型"，
  `background` 只是"fork 时前台等结果还是后台跑"。没有 `context: fork` 时，即使写了
  `agent` / `background` 也不派发，按常规路径注入。上游锚点：
  `vQt(e,n){if(n||_c()||Te())return!1;return e.background??!0}`；官方文档：
  `context` 只接受 `fork`，`agent` 是"context: fork 时使用的子代理类型"，
  `background` 是"仅配合 context: fork；默认 true"。
  1. **声明携带**：`CommandDefinition` / `SkillDefinition` 新增 `context`（str，默认
     空串）。解析只认 `"fork"`（去空白后精确匹配；其他非空值告警丢弃，非字符串
     告警丢弃；`null`/缺省/空串等同不声明），大小写不敏感不做，`Fork` 一律拒绝。
     `as_entry()` 携带，`expand_user_input` 的 `info` 载荷携带，`SkillDefinition.
     as_dict()` 仅在非空时写出。`COMMAND_UNSUPPORTED_FIELDS` / `SKILL_UNSUPPORTED_
     FIELDS` 从各自清单里去掉 `context`（`UNSUPPORTED_FIELDS` 全局仍保留它，仅两份
     派生清单排除）。
  2. **运行时闸门**：`runtime._dispatch_command_as_subagent` 的守卫从"`agent` 或
     `background` 任一非默认"改成"`context == "fork"`"；Skill 工具处理器
     （`tools/skills.py`）的派发条件从 `agent or background` 改成 `context == "fork"`。
     fork 分支里的 `agent`（缺省 general-purpose）与 `background` 语义不变。
  3. **偏离项**：`background` 缺省仍为 False（沿用 #311 口径），与上游"默认 true"
     不同；本批只改闸门、不动缺省，避免把"声明了 fork 但没写 background"的既有
     行为翻成后台。`test_command_context_fork_v312.py` 明确钉住这个偏离
     （`fork + agent`、无 background → 前台）。
  判据 `services/api/tests/test_command_context_fork_v312.py`（34 例：定义字段默认值/
  携带、frontmatter 解析各档（fork/其他值/null/非字符串/大小写 `Fork` 拒绝/空白去
  噪）、`as_entry` 与 expand info 携带、两份不支持清单排除、运行时 fork+agent→前台 /
  fork+background→后台 / fork 无 agent→general-purpose 前台 / agent 无 fork→不派发 /
  background 无 fork→不派发 / 无字段→不派发、Skill 侧的解析与 `as_entry`/`as_dict`、
  skill fork+agent→前台 / fork 无 agent→默认 / agent 无 fork→返回正文 /
  background 无 fork→返回正文 / fork+background→后台），变异 6/6 红（打断命令
  context 解析 → 9 红；拆运行时闸门 → 4 红；摘 expand info 的 context → 10 红；摘
  SkillCatalog 的 context → 10 红；处理器闸门退回 `agent or background` → 3 红；
  打断 Skill context 解析 → 1 红，红集合均实测），相邻命令/技能/代理/子代理套件连同
  本批 **125 passed**。

- **命令侧 `agent` / `background` 派发子代理（#311）**：对齐 Claude Code 的命令
  frontmatter 语义——命令声明了 `agent`（子代理类型名）或 `background: true` 时，
  展开出的正文不再注入主代理循环，而是派给一个子代理执行。`agent` 决定用哪个定义，
  `background` 决定前台等结果还是后台跑。两者都没声明时走常规注入。这是 #303 Skill
  侧的镜像：Skill 的 agent/background 走工具处理器，命令的走输入管道。
  上游锚点：`vQt(e,n){if(n||_c()||Te())return!1;return e.background??!0}` 判定是否
  派发，`EQt` 后台派发，`agent` 决定子代理类型。
  1. **声明携带**：`commands_defs.py` 的 `CommandDefinition` 新增 `agent`（str，默认
     空串）与 `background`（bool，默认 False）字段，`as_entry()` 一并携带；
     新增 `COMMAND_UNSUPPORTED_FIELDS`，从 `UNSUPPORTED_FIELDS` 里去掉 `agent` 与
     `background`——命令侧已支持，不再当"不支持"字段告警。frontmatter 解析对
     `agent: null` 留空不告警，非字符串告警丢弃；`background` 只认布尔，非法值告警
     丢弃。
  2. **展开管道**：`expand_user_input` 的 `info` 载荷携带 `agent` 与 `background`
     （未声明时为默认值，键仍在），运行时从 `request.expansion` 读出。
  3. **运行时派发**：`runtime.py` 新增 `_dispatch_command_as_subagent`，在
     `task.started` 之后、会话钩子之前调用。声明了 `agent` 或 `background` 时，
     用 `_resolve_agent` 解析子代理定义（未知类型 fail-closed，不静默降级），
     `_apply_definition` 挂上定义字段，前台走 `SubAgentRunner.__call__`、后台走
     `spawn_background`。返回证据包 dict 时调用方直接返回，不再走 `graph.ainvoke`；
     返回 None 时按常规路径继续。
  判据 `services/api/tests/test_command_agent_background_v311.py`（23 例：定义字段
  默认值/携带、frontmatter 解析各档（agent 字符串/null/非字符串、background
  true/false/非法、两者同声明）、as_entry 携带、expand info 携带与默认值、不支持
  清单排除、运行时前台派发/后台派发/无声明不派发/未知类型 fail-closed、_resolve_agent
  行为），变异 5/5 红（摘派发调用 → 前台/后台派发红；摘解析块 → 解析红；摘
  fail-closed → fail-closed 红；摘 info 键 → 展开红；摘无声明守卫 → 不派发红，
  红集合均实测），相邻命令/技能/代理/子代理套件连同本批 **128 passed**。

- **命令/Skill 的 `allowed-tools` / `disallowed-tools` 工具池收窄（#310）**：对齐上游
  命令 frontmatter 与 Skill 元数据的 `allowedTools` / `disallowedTools` 语义——声明了
  工具约束之后，本轮后续步骤的**可见工具表**与**可执行工具池**都按这份收窄。此前声明
  写了只进审计，模型照样看见、照样能调，约束形同虚设。这批把"声明→收窄→执行硬拦"
  整条链路接通，两层各自独立、互不替代。
  1. **声明携带**：`commands_defs.py` 的 `expand_user_input` 在命令 frontmatter 解析
     时把 `allowed_tools` / `disallowed_tools` 放进 `info` 载荷（未声明时为空列表，键仍在）；
     Skill 侧的 `allowed_tools` / `disallowed_tools` 由 SkillCatalog 解析携带。
  2. **用户侧入口（`/name` 展开）**：`runtime.py` 新增 `_turn_state_from_expansion`，从
     `request.expansion` 取两份列表构造 `turn_state` 初始值，传入 `ToolContext`。用户敲
     `/name args` 后，本轮后续步骤的工具池按这份收窄。
  3. **模型侧入口（`skill` 工具）**：`skills.py` 的 `_skill` 处理器在写 `skill_model`
     之后，把 Skill 声明的 `allowed_tools` / `disallowed_tools` 写进 `context.turn_state`。
     模型调用 skill 加载正文后，本轮后续步骤按这份收窄。
  4. **可见层**：`graph.py` 在下发工具 schema 前调用 `filter_specs_by_turn_state`，按
     `turn_state` 过滤 `specs`——被禁工具模型**看不见**。
  5. **执行层**：`gateway.py` 的 `execute` 在场景闸门之后、路径闸门之前，按 `turn_state`
     硬拦——不在 `allowed` 集合里 → `denied`；在 `disallowed` 集合里或 `disallowed`
     含 `*` → `denied`。审批放行也救不回来，这是"这份声明不准用"的硬禁，与场景同级。
  两层独立生效：可见层管"模型看得见什么"，执行层管"模型调得动什么"，任一层被摘掉，
  另一层照常兜底。`*` 在 disallowed 里表示禁用全部工具。
  判据 `services/api/tests/test_command_tool_restrictions_v310.py`（20 例：命令展开载荷
  携带工具约束、运行时从 expansion 构造 turn_state、skill 处理器写 turn_state、可见层
  specs 过滤、执行层 allowed 硬拦 / disallowed 硬拦 / `*` 全禁 / 无约束放行、两层独立
  生效的正反两路），变异 5/5 红（摘网关执行层硬拦 → 3 红；摘 `filter_specs_by_turn_state`
  调用 → 2 红；摘运行时 expansion→turn_state 接线 → 1 红；摘 skill 处理器 turn_state
  写入 → 4 红；摘命令展开载荷工具键 → 5 红，红集合均实测），相邻命令/技能套件
  **295 passed**、相邻网关/代理/权限套件 **231 passed**。

- **SessionStart 钩子输出四字段（#304）**：对齐上游 `Sn()` 校验函数定义的
  SessionStart 钩子输出四字段，此前钩子作者写出这四个字段只会被静默丢掉——
  正文进了审计但没有任何消费端。这批把四字段的解析、组装、审计与消费端全部接通。
  上游锚点（逐字取证自 `claude.uniq.txt` 的 `Sn()`）：`sessionTitle` / `initialUserMessage`
  为字符串字段，`watchPaths` 为字符串数组（经 `i1t()` 校验），`reloadSkills` 为布尔、
  只认 `true`。
  1. **`hooks.py` 四字段解析与组装**：`HookResult` 新增 `session_title` /
     `initial_user_message` / `watch_paths` / `reload_skills` 四个字段（含默认值）；
     `parse_hook_output` 对齐上游 `Sn()` 的类型校验——`sessionTitle` /
     `initialUserMessage` 只认字符串、非字符串即错型不解析；`watchPaths` 只认字符串数组、
     非数组或含非字符串元素即错型不解析；`reloadSkills` 只认真布尔 `true`，
     `false` / 字符串 `"true"` / `1` 都当写错的钩子、不解析（把它读成"要重载"等于让一次
     错型变成一次静默的目录重扫）。同时支持 `hookSpecificOutput` 嵌套形状与顶层字段两种
     写法。`as_dict` 序列化：空字段省略、`initial_user_message` 只记字符数、
     `watch_paths` 只记条数。`_finish` 把四字段从解析结果装配进 `HookResult`。
  2. **`runtime.py` 四字段消费端**：`sessionTitle` → 经 `Task.thread_id`（UUID FK）
     更新 `Thread.title`（检查点 thread_id 是 `task:<id>:attempt:<n>` 格式，不是 UUID，
     必须经 Task 行桥接）；`initialUserMessage` → 透传 `initial_messages()`；
     `watchPaths` → 写入 `.blank/context/watch.json`；`reloadSkills` → 重扫磁盘 Skill 与
     命令目录、重建 `SkillCatalog` 并替换 `session.skill_catalog`。四字段各自独立消费，
     互不影响：一个钩子只给其中一两个也照常生效。各自落审计事件
     （`hook.session_title` / `hook.watch_paths` / `hook.skills_reloaded`）。
  3. **`graph.py` 首条消息注入**：`initial_messages()` 新增 `initial_user_message` 参数，
     有值时在系统提示之后、历史之前插入一条 user 消息（`[:8000]` 截断）。
  判据 `services/api/tests/test_session_start_output_v304.py`（22 例：纯解析类型校验
  正反两路、HookResult 组装与默认值、as_dict 序列化、initial_messages 注入位置、
  HTTP 端到端四字段各自消费与无字段零副作用），变异 8/8 红（解析层四字段各拔一条 →
  4-5 红，消费层四字段各拔一条 → 1 红，每条红集合均实测），相邻钩子/代理套件
  **57 passed**，全量回归见下方读数。

- **agent_transcript_path 补齐（#305）**：对齐上游 `SubagentStop` / `StopFailure`
  载荷里的 `agent_transcript_path`。此前两个事件都已注册、都能触发，但载荷里没有这个
  字段——本机根本没有 transcript 落盘机制，钩子拿到路径也没东西可读。这批把"触发点
  落盘 + 载荷带路径"接通。
  1. **`hooks.py` 落盘助手**：新增 `write_transcript(messages, path)`，把对话消息写成
     JSONL 快照——每行一条 `{"role", "content"}`，与上游的 JSONL transcript 格式一致。
     content 非字符串时 `str()` 兜底（工具返回的列表/对象不会让写盘炸掉），空消息列表
     写空文件，父目录自动创建，返回绝对路径字符串。
  2. **`subagent.py` `stop_gate`**：触发 `subagent_stop` 钩子前，把子代理的 `messages`
     写成 `.blank/context/subagent-transcript.jsonl`（与 #304 的 `watch.json` 同目录），
     载荷带 `agent_transcript_path`。没配钩子时 `stop_gate` 直接返回，不写文件。
  3. **`runtime.py` `_fire_stop_failure`**：读取检查点消息时，把 `messages` 写成
     `.blank/context/agent-transcript.jsonl`，载荷带 `agent_transcript_path`。该字段与
     `last_assistant_message` 同在 try 块里——读不到检查点就少一个可选字段，事件照发。
  判据 `services/api/tests/test_agent_transcript_path_v305.py`（10 例：write_transcript
  四单元（JSONL 格式 / 非字符串 coercion / 空消息 / 父目录创建）、事件面注册、SubagentStop
  端到端载荷带路径且文件内容逐行 JSON 含 goal 与助手回复、无钩子不写文件、StopFailure
  端到端载荷带路径、transcript 反映检查点消息、无钩子零改动），变异 4/4 红（摘
  SubagentStop 路径 → 1 红；摘 StopFailure 路径 → 2 红；write_transcript 不写文件 → 7 红；
  去掉非字符串 coercion → 1 红，红集合均实测），相邻钩子/代理套件 **73 passed**。

- **MessageDisplay 钩子（#306）**：对齐上游 MessageDisplay 事件——每批流式输出展示给
  用户时触发，仅展示层：落库的消息与模型上下文一字不改。上游语义（逐字取证自
  `claude.uniq.txt`）：载荷含 `turn_id` / `message_id` / `index` / `final` / `delta`
  五字段，输出 `displayContent`（从 `hookSpecificOutput` 读）替换展示文本，省略则照原
  样展示，钩子抛异常时回落原文（"emitting original text"），非阻断（不在 BLOCKING_EVENTS）。
  1. **`hooks.py` 注册与解析**：`HOOK_EVENTS` 加 `message_display`，`EVENT_ALIASES` 加
     `MessageDisplay` 别名；`HookResult` 加 `display_content` 字段，`as_dict` 按非空串
     序列化 `display_content_chars`；`parse_hook_output` 只认字符串类型的 `displayContent`
     ——非字符串是写错的钩子，把它读成"要替换"等于让一次错型变成一次静默的界面篡改。
  2. **`live.py` OutputRelay 触发点**：`_flush` 与 `close` 改为 async，每帧展示前调用
     `display_hook` 回调，传五字段载荷（`index` 用 `_flush_index` 递增、`message_id` 用
     每 relay 唯一 uuid4），返回非空串就替换展示文本，空串或异常回落原文。无钩子时
     `_make_relay` 传 `display_hook=None`，`_flush` 走纯发布路径零开销。
  3. **`graph.py` 接线**：`_make_relay` 检查 `self.hooks` 非空且注册了 `message_display`
     才挂回调，回调跑 `hooks.run("message_display", node, payload)`，取第一个
     `display_content` 非空的 HookResult；三处 `relay.close()` 改为 `await relay.close()`。
  判据 `services/api/tests/test_message_display_v306.py`（16 例：事件注册与别名、非阻断
  地位、`parse_hook_output` 的 displayContent 类型校验（hookSpecificOutput / 顶层 /
  非字符串拒绝 / 空串省略 / 缺席不替换）、HookResult 组装与 as_dict 序列化、OutputRelay
  无钩子发布原文 / 有钩子替换 / 钩子失败回落 / 空返回保原文 / index 递增、端到端
  `_call_adapter` 有钩子替换帧与无钩子保原文），变异 3/3 红（拔 displayContent 解析 →
  3 红；拔钩子触发 → 3 红；拔文本替换 → 2 红，红集合均实测），相邻钩子/流式套件
  **57 passed**。

- **思考强度（effort）五档裁决与三路下发（effort 批）**：Claude Code 的 thinking-effort
  系统是"本轮思考强度"的一整套裁决与下发链路，从用户写的档位词、模型封顶、关键词升档、
  thinking 关断回落，到最后落在模型载荷、提示词占位符、子进程环境变量三条路上。这批把
  整条链路移植进仓库。上游锚点（逐字取证自 `claude.uniq.txt`）：五档词表
  `low/medium/high/xhigh/max`、默认档 `high`、`med` 别名归一、关键词
  `ultracode`/`ultrathink` 升到 `xhigh`、`maxEffortLevel` 封顶、thinking 关断时
  钳到 `high`、`${CLAUDE_EFFORT}` 占位符替换、`CLAUDE_EFFORT` 环境变量写进子进程。
  1. **`effort.py` 裁决模块**：`EFFORT_LEVELS` 五档词表、`EFF_RANK` 排序、
  `LEVEL_ALIASES` 别名归一、`KEYWORD_EFFORTS` 关键词升档表、`THINKING_DISABLED_EFFORT_CAP`
  关断钳位。`EffortPolicy` 数据类承载封顶三要素（`model_caps` 模型表 / `provider_cap`
  供应商档 / `default_cap` 缺省档），`effective_cap` 取家族与供应商里更严的那个，
  `clamp` 把超出封顶的档位往下推、`allows`/`allowed_levels` 按封顶给可用集，
  `supports_xhigh`/`supports_max` 区分"模型支持但被封顶"与"模型根本不支持"。
  `ThinkingConfig` 数据类承载 thinking 开关与预算，`disabled` 属性判关断，
  `budget_tokens` 天花板是 `max_tokens - 1`。`resolve_turn_effort` 是整轮裁决的唯一入口：
  关键词优先 → 用户请求 → 封顶钳制 → thinking 关断回落，返回 `TurnEffort`
  （含最终档位、请求档位、警告文案、提醒文案、是否来自关键词、是否在封顶内）。
  `clamp_without_thinking` 只在"thinking 关断 + 模型拒绝高阶 + 档位 > high"三个条件
  同时成立时才钳，缺一不可。`exceeds_default_only_true_for_levels_above_high` 钉住
  "高于 high 才算超出默认"的边界。
  2. **`${CLAUDE_EFFORT}` 占位符**：`prompt_args.py` 的 `substitute_paths` 在 `effort`
  非空时替换占位符，传的是**裁决后**的档位（过封顶、过钳制之后的终值），不是用户原始
  请求。`render_prompt` 把 effort 透传到替换层，Skill/斜杠命令的参数正文因此能拿到
  本轮真实强度。
  3. **`CLAUDE_EFFORT` 环境变量**：`shell_env.py` 的 `apply_effort` 把裁决后的档位写进
  子进程环境，只在非空时写键、空值不写——避免给子进程一个无意义的空字符串。钩子与
  命令的子进程因此能读到本轮强度。
  4. **适配器载荷三方言**：Anthropic 走 `output_config.effort` + `thinking` 两键；
  OpenAI-compat 走 `reasoning_effort`，`xhigh`/`max` 映射到 `high`（该方言只认三档），
  空值回落到供应商配置的 `reasoning_effort`；OpenAI-responses 走 `reasoning.effort`，
  四档词表内直接发，超出的映射到 `high`，命中时附带 `reasoning.encrypted_content`
  include 且不发 temperature。三方言的映射口径与上游逐字对齐。
  5. **运行时接线**：`graph.py` 的 `AgentSession` 新增 `turn_effort`/`effort_reminder`/
  `effort_warning` 三个字段；`_resolve_turn_effort` 在回合开始时裁决一次并缓存，
  之后每一步调用复用同一档位（一轮的强度不逐步重算、不逐步变）。压缩路径在压缩后
  重新裁决并把警告与提醒分别送进事件与系统消息。八个钩子调用点（权限请求、停止门、
  压缩前、子代理停止等）在调用钩子时把 `effort=session.turn_effort` 透传，钩子子进程
  因此能读到本轮强度。
  6. **子代理强度**：`subagent.py` 在派发子代理时用与主代理同一套 `resolve_turn_effort`
  裁决子代理的档位（定义里声明的档位 + 目标正文里的关键词），裁决一次后整轮复用。
  定义没声明时回落到供应商配置的静态 `reasoning_effort`，与主代理同一份口径——同一家
  供应商在主线程与子代理里不会跑出两个档位。
  7. **判据**：`services/api/tests/test_effort_batch.py`（25 例，七区）：档位词表与别名
  归一、封顶取严与可用集、钳制下压与警告文案、关键词升档与提醒、`${CLAUDE_EFFORT}`
  占位符替换与透传、`CLAUDE_EFFORT` 环境变量写入条件、三方言载荷映射、thinking 关断
  回落的三条件、预算天花板、ThinkingConfig 解析、超出默认的边界。每条判据都在"拿掉
  对应实现就会红"的位置上。变异电池 `evals/effort_mutation_battery.py`（9 条变异，
  红集合登记在 `evals/fixtures/battery-booked-effort.json`，第二轮逐字核对通过）：
  M1 钳制不压 → 3 红；M2 封顶取松 → 1 红；M3 关键词不抬档 → 2 红；M4 占位符不替换
  → 2 红；M5 环境键不写 → 1 红；M6 anthropic 不下发 effort → 1 红；M7 thinking 关断
  不回落 → 1 红；M8 词表被改 → 2 红；M9 openai_compat 不映射 → 1 红。跑完按原字节
  还原并核对 sha256。邻套件回归：effort 接线触到的钩子/子代理/团队/压缩/保留期等
  20 个套件 **341 passed**，全量 **3621 passed / 28 skipped**。
  改 `services/api/src/blank_api/services/effort.py`（新增）、`prompt_args.py`、
  `shell_env.py`、`models/anthropic.py`、`models/openai_compat.py`、
  `models/openai_responses.py`、`agent/graph.py`、`subagent.py`、`agents_defs.py`、
  `commands_defs.py`、`hooks.py`、`skills_defs.py`、`tools/base.py`、`tools/delegate.py`、
  `tools/gateway.py`、`tools/shell.py`、`tools/skills.py`、`agent/runtime.py`，
  新增 `services/api/tests/test_effort_batch.py`、`evals/effort_mutation_battery.py`、
  `evals/fixtures/battery-booked-effort.json`。
  偏离/未移植：`EffortPolicy` 的 `model_caps`/`provider_cap`/`default_cap` 目前以空表
  构造（配置来源尚未接进——裁决逻辑本身完整，接上配置源即可生效）；上游的
  `maxEffortLevel` 模型表逐模型封顶在本仓库还没有配置面，先以"空表 = 不封顶"的口径
  跑通裁决链，配置面留待后续批次接入。

- **Skill frontmatter `agent` / `background`（#303）**：对齐上游 `lme` 的
  `agent:O.agent!=null?String(O.agent):void 0` 与 `background:m1(O.background)`
  口径——声明 `agent` 时 Skill 正文不注入主线程、而是作为 goal 派发对应类型的子代理
  执行（前台等结论），声明 `background: true` 时后台派发并返回句柄（`agent_id`、
  `status="forked"`、`background=True`）。命令侧的 `agent`/`background` 不在本批
  范围内（`expand_user_input` 是同步的、拿不到 `subagent_runner`），仍留在
  `UNSUPPORTED_FIELDS` 只审计不生效。
  1. **解析**：`skills_defs.py` 的 `_definition_from_dir` 读 frontmatter 的
  `agent`（非空字符串才保留、空串与 null 等价、非字符串告警丢弃）与 `background`
  （只认 `true`、`false`/缺省/null 等同不声明、非布尔告警丢弃），值传进
  `SkillDefinition`（`agent: str = ""` / `background: bool = False`），
  `as_entry()` / `as_dict()` 原样带出。`ignored` 检查从 `UNSUPPORTED_FIELDS`
  切换到 `SKILL_UNSUPPORTED_FIELDS`（后者排除 `agent`/`background`），命令侧
  不受影响。
  2. **派发接线**：`tools/skills.py` 的 `_skill` handler 在 `skill.invoked`
  记录之后、主线程注入之前插入子代理派发分支——`agent` 或 `background` 任一
  声明即走子代理：`_resolve_agent` 按名字查 `context.agents` 目录（空名字回落
  到默认 `general-purpose`，未知类型 fail-closed 并列出可用名字），
  `_apply_definition` 把定义字段写进 `SubAgentRequest`，`background: true`
  走 `runner.spawn_background` 返回句柄、否则前台 `runner(request)` 等结论。
  无 `subagent_runner` 或后台运行时不支持 `spawn_background` 时明确报错，
  不静默回退到主线程注入。子代理执行失败把子代理的 `error` 透传为
  `fail("error", …)`。延迟导入 `subagent`/`delegate` 避免循环（`subagent.py`
  在模块级 import 了本模块的 `SkillCatalog`）。
  3. **判据**：`services/api/tests/test_skill_agent_background_v303.py`（29 例，
  七区）：frontmatter 解析（agent 非空/去空白/非字符串丢弃、background 布尔
  三态/非法告警）、`SKILL_UNSUPPORTED_FIELDS` 与 `UNSUPPORTED_FIELDS` 的包含
  关系（skill 侧不含、命令侧仍含）、`as_entry`/`as_dict` 字段携带、`SkillCatalog`
  条目携带与缺省、handler 派发（前台子代理收到正文、主线程不注入、未知类型
  fail-closed、无 runner 报错、后台派发返回句柄、`background:false` 回落注入、
  两字段均缺省回落注入）、`discover_skills` 端到端携带。变异电池 4 条：
  M1 移除 agent 检查 → 4 红；M2 移除 background 检查 → 2 红；M3 无字段也派生
  → 2 红；M4 background/foreground 互换 → 2 红。邻套件回归 473 例全绿。
  改 `services/api/src/blank_api/services/skills_defs.py`、
  `services/api/src/blank_api/services/tools/skills.py`，
  新增 `services/api/tests/test_skill_agent_background_v303.py`。
  偏离/未移植：命令侧的 `agent`/`background` 未移植（输入管道无异步上下文、
  拿不到子代理运行时）；上游 Skill 的 `agent` 指向自定义 agent 类型，本批
  与子代理目录解析同口径，插件 agent 的名字回退（文件名/描述）已在 Y 批落地。

- **`UserPromptExpansion` 与 `TeammateIdle` 两个事件、钩子类型补到六种（AF 批）**：事件面
  在 AE 批之后还剩这两个没移植，钩子类型此前只支持 `command`——等于"能写钩子，但上游
  能配的四类（prompt/agent/http/mcp_tool/script）配了也认不出来"。这批把两件事一次收掉。
  上游锚点（逐字取证自 `claude.uniq.txt`）：`UserPromptExpansion` 的载荷五键
  `expansion_type/command_name/command_args/command_source/prompt`、`matcher` 的
  `fieldToMatch` 是 `source`、退出码 2 即"这次展开作废"；`TeammateIdle` 载荷两键
  `teammate_name/team_name`（上游把 team_name 标 deprecated，仍按会话推导传）、
  `preventContinuation` + `stopReason`、缺省文案 `TeammateIdle hook prevented
  continuation`；六种类型的序列化形状 `{type:"http",command:url}` /
  `{type:"mcp_tool",command:"${server}/${tool}"}` / `{type:"prompt",prompt}` /
  `{type:"agent",prompt}` / `{type:"script",file|script}`，执行分发
  `kn.type==="command"||"http"||"mcp_tool"||"script"` 与
  `kn.type==="agent"||"prompt"`，http 走 `dfe`（POST JSON、非 2xx 文案
  `HTTP ${statusCode} from ${url}`、**成功时把 statusCode 记成 exitCode 而 outcome 是
  success——退出码不等于成败**），script 型 `Jst` 直接抛
  `script hooks are not available in this build`，prompt/agent 型无会话上下文时抛
  `prompt-type hooks are not supported for ${event} events (no conversation context is
  available). Use a command-type hook instead.`。
  改 `services/hooks.py`、`services/commands_defs.py`、`services/agent/runtime.py`、
  `services/agent/graph.py`、`services/team.py`、`services/api/routes/tasks.py`、
  `models/task.py`、`schemas/task.py`（新增 `expansion` 列 + 迁移 `0036_task_expansion.py`）。
  1. **事件面**：`HOOK_EVENTS` 增 `user_prompt_expansion`、`teammate_idle`（20 → 22），
     别名 `UserPromptExpansion` / `TeammateIdle`；`user_prompt_expansion` **进**
     `BLOCKING_EVENTS`（与 `user_prompt_submit` 同族——不进集合的话退出码 2 会被当成
     一条非阻断错误，否决直接失效），`teammate_idle` **不进**（上游把退出码 2 渲染成
     一条"钩子报错"，进集合就会变成"成员不能转空闲"）。
  2. **`user_prompt_expansion` 接线**：载荷五键由 `expansion_payload` 从 `expand_user_input`
     的 info 里抽出（多一个少一个都会让"钩子没拿到参数"查不出来），`matcher` 比
     `expansion_type`；触发点两处——提交路径（`runtime._run_expansion_hooks`）与插话/steer
     路径（`graph` 读 `steer.queued`），载荷与处置不分叉。三种处置：否决（退出码 2 /
     decision=deny）作废整次展开、理由带用户原文；`suppressOriginalPrompt` 时原文从阻断
     消息里略去（只抑制上下文，不落账）；`ask` 档在提交这一步没有确认界面，按否决处理
     （静默放行等于把钩子写下的条件丢掉）。补上下文与提交钩子的补段一起进模型。
     展开事实随行落库：任务行新增 `expansion` 列（迁移 `0036_task_expansion.py`）存展开
     那一刻的整份事实，`TaskOut` 原样带出——钩子在执行那一刻才跑（可能在另一个进程、也
     可能在重启之后），读任务行比回头解析审计时间线稳；且审计事件落库前过 `mask_deep`，
     密钥形态的参数（`--token=sk-…`）会被整段抹掉，钩子要读的正是用户真写的那份参数，
     落列才保得住原值。插话（steer）没有可落的列，走审计事件，落库前同样过 `mask_deep`。
  3. **`teammate_idle` 接线**：`team.py` 成员即将转入空闲那一刻触发，载荷两键逐字，
     `matcher` 比 `teammate_name`；`preventContinuation` 阻止继续、理由取 `stopReason`、
     缺省用上游那句；补上下文的去处是群记录（下一位发言者与用户读的都是它），审计另记
     count/chars；退出码 2 只算一条非阻断错误（可见、不注入、不阻止）。
  4. **钩子类型六种**：`HOOK_KINDS` 补 `prompt/agent/http/mcp_tool/script`；
     `_build_specs` 各类型取各自的主键（http→`url`、mcp_tool→`server`/`tool`、
     script→`file` 或内联正文），`_execute` 按 `spec.kind` 分发，四类的输出判定与退出码
     语义共用 `_finish`。缺关键字段（http 缺 url、mcp_tool 缺 server/tool、prompt 缺
     prompt、script 缺 file 且无内联正文）逐条告警跳过，告警不带命令正文。
  5. **http 的成败语义**：非 2xx 逐字 `HTTP ${statusCode} from ${url}`；2xx 时上游把
     statusCode 记成 exitCode、body 当 stdout，而成败看 outcome（2xx 即 success）——
     退出码不等于成败，`_finish` 新增 `ok` 参数显式给定，**不能拿"非 0 即失败"反推**
     （否则 200 会被误判成失败）。
  6. **prompt/agent 型**：无会话上下文时逐字报上游那句（agent 型前缀换 `agent-type`），
     作为一条非阻断错误（可见、不阻断、不注入），而不是静默跳过；注入 `prompt_runner`
     时把提示词交给模型、正文按同一套解析。
  7. **mcp_tool 型**：`${server}/${tool}`；上游从会话上下文取 MCP 客户端，未注入
     `mcp_runner` 时报"缺 MCP 客户端"的非阻断错误，而不是把它当命令去跑。
  8. **script 型**：上游这个构建里 `Jst` 直接抛 `script hooks are not available in this
     build`；这里认出来、执行时按同一句逐字报一条非阻断失败（不执行脚本文件）。
  偏离/未移植：prompt/agent/mcp_tool 型在本仓库还没有"把提示词/工具调用送进会话上下文"
  的注入点（`runtime` 未接 `prompt_runner` / `mcp_runner` 时按上游同口径报缺上下文，而非
  静默跳过——接上注入点后这两类即可真正执行）；上游的 `function`/`callback`/`unknown`
  类型不在执行分发里，不移植；`TeammateIdle` 的 `team_name` 上游已 deprecated，仍按会话
  推导传。
  判据 `services/api/tests/test_hook_triggers_af3ab.py`（17 例：两事件注册/别名/阻断归属、
  五键逐字、两份载荷键常量不漂移、matcher 按 `expansion_type`/`teammate_name` 分流、
  否决带原文、抑制不泄漏原文、ask 按否决、补上下文与来源掩码、阻止继续与缺省文案、
  退出码 2 非阻断分流、stdin 载荷）与 `test_hook_kinds_af3c.py`（18 例：六种类型识别与
  字段对齐、缺参告警、未知类型跳过、http 真 POST 的 2xx 判成功且退出码记状态码、非 2xx
  文案逐字、mcp_tool/prompt/agent 注入 runner、script 执行报上游那句）；另修正
  `test_hook_events_v52.py`（阻断集合补 `user_prompt_expansion`、`HookSpec` 位置传参回归）
  与 `test_settings_hooks_v53.py`（未知事件/未知类型告警、未知类型在已知事件内告警跳过）。
  变异电池 `evals/hooks_mutation_battery_af.py`（8/8 红，红集合登记在
  `evals/fixtures/battery-booked-af.json`、第二轮逐字核对通过，跑完按原字节还原并核对
  sha256）：M1 http 2xx 去掉 `ok=True` → 2 红；M2 `HOOK_KINDS` 摘 `script` → 3 红；
  M3 script 措辞改坏 → 1 红；M4 `_finish` 忽略 `ok` → 2 红；M5 `user_prompt_expansion`
  摘出阻断集合 → 2 红；M6 展开钩子忽略 `suppress_original` → 1 红；M7 idle 把退出码 2
  当阻止 → 1 红；M8 idle 缺省文案改坏 → 1 红；相邻钩子/子代理/设置套件 **248 passed**
  （74.40s），全量 **3596 passed / 28 skipped（788.81s，RC=0）**。

- **SubagentStart 钩子与 SessionStart 的附加上下文（AD 批）**：钩子作者在子代理派发点能
  拦一句话，而 `SessionStart` 给的 `additionalContext` 此前只进审计、正文被丢掉——等于
  "配了也不生效"。上游锚点：`SubagentStart` 非阻断、matcher 比 `agent_type`、退出码 2 由
  消费方渲染成 `hook_non_blocking_error`（target 形如 `SubagentStart:<agentType>`，
  退出码 2）、`additionalContexts` 收集后以 `hook_additional_context`
  attachment 注入。改 `services/hooks.py`、`services/subagent.py`、
  `services/agent/runtime.py`、`services/agent/graph.py`。
  1. **事件面**：`HOOK_EVENTS` 增 `subagent_start`（18 → 19），别名 `SubagentStart`；
     `BLOCKING_EVENTS` 不变——进集合就会把"钩子报错"变成"派不出去"。settings 的嵌套
     形状与 `BLANK_HOOKS` 的扁平形状都能配，`from_env` 不报警告。
  2. **派发接线**：`run_subagent` 在 `ToolContext` 之后、压缩器之前跑一次，target 是
     `request.agent_name or general-purpose`；stdin 带 `agent_id` / `agent_type` /
     `agent_path` / `depth` / `goal` / `parent_thread_id` / `root_thread_id` /
     `workspace` / `permission_mode` / `fork`。
  3. **注入**：`additionalContext` 拼成一条独立 user 消息（标记头 `<subagent-start-hook>`），
     排在 Skill 预加载之后、goal 之前；正文写明"只补充信息，不得放宽规则/审批/工作区
     边界"。单条夹到 `MAX_CONTEXT`、合并后截断；`hook.context_received` 落审计。
  4. **exit 2 的两种用途必须分开**：`_execute` 对非阻断事件的 exit 2 会把 stderr 并进
     `additional_context`，若直接注入，一条"钩子报错"就成了子代理的作业指令。新增
     `usable_context` / `non_blocking_errors` 按 `exit_code` 分流（两个接线点共用）：
     退出码 2 只进 `SubAgentResult.hook_warnings`（`render()` 带【子代理钩子告警】，前台
     回执与后台 `task_output` 都看得到）并落 `hook.non_blocking_error`，不注入上下文。
     子代理照常启动、照常交付。
  5. **续聊不重跑**：`send_message` 是同一个子代理接着说话，`_run_prepared` 显式传
     `continuation=True` → `start_hooks=False`；fork 仍按新派发跑（它拿的是父会话快照，
     本来就不是续聊）。历史上已注入的那条消息随对话保留，不重复贴。
  6. **SessionStart 正文进模型**：`_fire_hooks` 改为返回结果列表；`usable_context` 的正文
     作为独立 user 消息进 `initial_messages`（位置在 carried 历史之后、本轮目标之前，
     `[:8000]`，写明只作参考、不得覆盖安全/审批/工作区边界），落 `hook.context_injected`；
     退出码 2 走 `hook.non_blocking_error`、不注入。目标原文一个字不动。
  偏离：上游 `isolatedContext` 子代理跳过注入、`hookContextCarried` 去重未移植（续聊整轮
  不重跑，效果等价；隔离副本仍会收到）；stdin 未提供 `served_call`（语义不明、不在官方
  文档里，留作文档偏离）。
  判据 `services/api/tests/test_subagent_start_hooks_v54.py`（9 例：事件面与别名、exit 2
  分流单元、真子进程按 agent_type 分发、注入位置与 goal 原文、exit 2 可见但不注入、
  无钩子零改动、续聊不重跑、SessionStart 正反两路 HTTP 端到端），变异 7/7 红（M1
  去掉退出码分流 → 3 红；M2 续聊无条件重跑 → 1 红；M3 graph 丢弃 hook_context → 1 红；
  M5 结果丢 `hook_warnings` → 1 红；M6 target 换成 agent_id → 2 红；M7 注销事件 → 4 红；
  每条红集合均实测）；相邻钩子/子代理/设置套件 **195 passed**（47.30s），全量
  **3552 passed / 28 skipped（721.02s）**。

- **SubagentStart 钩子 stdin 字段补齐（#308 批）**：AD 批的 SubagentStart 载荷只带了
  十个字段，上游还有六个常用字段缺位——钩子作者拿到 `session_id` / `transcript_path` /
  `scratchpad_dir` / `prompt_id` / `caller_session_id` / `effort` 才能写"查这次派发的
  会话""读 transcript""往 scratchpad 写中间产物"这类钩子。上游锚点：官方 hooks 文档的
  SubagentStart 输入里这六个字段与十个已有字段同在。改 `services/subagent.py`。
  1. **`session_id`** → 派发用的 `task_id`（子代理的会话标识就是这一轮的作业 ID）。
  2. **`transcript_path`** → `str(run_root / ".blank" / "context" / "subagent-transcript.jsonl")`，
     与 SubagentStop 的 `agent_transcript_path` 同一个路径——钩子在启动时就能拿到
     将来收尾时会落盘的 transcript 位置。
  3. **`scratchpad_dir`** → `str(run_root / ".blank" / "context" / "scratchpad")`，
     在派发点 `mkdir(parents=True, exist_ok=True)`——上游给钩子一个可写的草稿目录，
     不创建就等于"给了路径但没给地方"。
  4. **`prompt_id`** → `uuid.uuid4().hex`，每次派发一个新 UUID——上游把它当本次提示
     的唯一标识，钩子可据此做幂等或去重。
  5. **`caller_session_id`** → `parent_thread_id`（发起这次派发的父会话线程）。
  6. **`effort`** → `{"level": child_effort.level}`（对象，与官方文档一致），同时
     `hooks.run(..., effort=child_effort.level)` 把同一档位作为 `CLAUDE_EFFORT` 写进
     钩子子进程环境变量——两条出口同源，钩子脚本里 `$CLAUDE_EFFORT` 与 payload 里的
     `effort.level` 永远一致。
  偏离：`served_call` 语义不明、不在官方文档里，仍不实现、只记文档偏离。
  判据 `services/api/tests/test_subagent_start_stdin_v308.py`（8 例：六个字段各一条
  可失败判据 + 一次性到位 + `CLAUDE_EFFORT` 子进程环境变量），变异 8/8 红（M1 去掉
  session_id → 2 红；M2 去掉 transcript_path → 2 红；M3 去掉 scratchpad_dir → 2 红；
  M4 去掉 prompt_id → 2 红；M5 去掉 caller_session_id → 2 红；M6 去掉 payload effort
  → 2 红；M7 去掉 hooks.run 的 effort 参数 → 1 红；M8 去掉 scratchpad mkdir → 1 红；
  每条红集合均实测）；相邻钩子/子代理套件 **41 passed**（18.74s）。

- **`!` shell 执行块（#309 批）**：Claude Code 在渲染命令文件正文时，把行首 ``!``
  开头的行当 shell 命令跑，stdout 嵌回正文替换掉 ``!command`` 那一行。此前本机把
  ``!`` 行原样留在正文里，模型读到的是命令文本而不是命令输出。改
  `services/prompt_args.py`。
  1. **`execute_shell_blocks(text, *, root, timeout=10.0)`**：正则 ``^!(.+)$``
    （MULTILINE）只匹配行首 ``!``；行中 ``!``（如 "不是 !echo"）不动。``root``
    为空时没有工作区可跑，``!`` 行原样保留——不猜、不抛。每条匹配用
    `tools/shell.py` 的 `_run_blocking` 执行（`shell=True`、`build_env()`、
    `normalize_relative_executable` 补相对路径），stdout `strip()` 后嵌回；
    退出码非 0 时嵌 `[命令失败 exit=N: stderr]`；超时嵌 `[命令超时: cmd]`。
  2. **`render_prompt` 接线**：`expand_arguments` → `substitute_paths` →
     `execute_shell_blocks`，保证 ``$ARGUMENTS`` 与 ``${CLAUDE_PROJECT_DIR}``
     等占位符先展开、再执行 ``!`` 行。`project_dir` 作为 `root` 传入，
     `render_skill` 与 `expand_user_input` 两条路径都覆盖。
  判据 `services/api/tests/test_command_shell_blocks_v309.py`（10 例：执行嵌回、
  多行独立执行、`$ARGUMENTS` 先展开、`${CLAUDE_PROJECT_DIR}` 先替换、行中 `!`
  不动、失败嵌错误、空 root 跳过、单独 `!` 不动、`expand_user_input` 端到端、
  `render_prompt` 空 project_dir），变异 5/5 红（M1 去掉 render_prompt 里的调用 → 3 红；
  M2 去掉正则 ``^`` 锚点 → 1 红；M3 去掉 ``if not root`` 守卫 → 2 红；
  M4 ``shell=True`` 改 ``False`` → 6 红；M5 去掉退出码检查 → 1 红；每条红集合均实测）；
  相邻命令/插件/Skill 套件 **109 + 29 passed**。

- **StopFailure 钩子事件：API 错误终结回合时顶掉 Stop（AE 批）**："回合因为 API 错误
  结束"与"正常结束"是两件事，钩子作者需要分清"被拦下"和"模型挂了"——两者的处置完全
  不同。此前本机两条 `except ModelError` 终态既不发 Stop 也不发任何事件，这条信息没有
  出口。上游锚点：`summary:"When the turn ends due to an API error"`、
  `description:"Fires instead of Stop when an API error (rate limit, auth failure, etc.)
  ended the turn. Fire-and-forget — hook output and exit codes are ignored."`、
  `matcherMetadata:{fieldToMatch:"error"}`、十三个分类取值，载荷
  `{hook_event_name, error, error_details?, last_assistant_message?}`。
  改 `services/hooks.py`、`services/agent/runtime.py`。
  1. **事件面**：`HOOK_EVENTS` 增 `stop_failure`（19 → 20），别名 `StopFailure`；
     **不进** `BLOCKING_EVENTS`——上游明说 fire-and-forget，退出码 2 不能变成"拒绝执行"，
     否则一个已经因模型失败而收尾的任务，会被钩子的报错再判成"被拦下"。
  2. **错误分类翻译层**：新增 `stop_failure_error_category(code, status)`，**状态码优先**
     （429→`rate_limit`、401/403→`authentication_failed`、402→`billing_error`、
     404→`model_not_found`、400→`invalid_request`、503→`overloaded`），再按 code 兜底
     （`model_timeout` / `model_unreachable` / `model_stream_error` /
     `model_stream_empty` / `model_http_error`→`server_error`、
     `model_output_not_json` / `model_tools_unsupported`→`invalid_request`、
     `circuit_breaker_open`→`overloaded`、`all_models_failed`→`unknown`），都落不上→
     `unknown`。状态码必须压过 code：带 429 的传输层失败要记成"被限流"，否则用户看不到
     这一层。分类表与上游十三个取值逐字相同（探针实测 21 组映射）。
  3. **触发接线**：run 路径（runtime.py:1215）与续跑路径（runtime.py:1306）两条
     `except ModelError` 终态，都在 `_finish` 之后、`_close_hooks` 之前调
     `_fire_stop_failure`（runtime.py:823）；载荷带 `error` 分类、`error_details`
     （错误文本前 500 字）、`last_assistant_message`（图里最近一条助手文本前 4000 字；
     读不到就省略该字段、事件照发，只留一条 warning）。**不发 Stop**——上游是"替代"，
     不是既发 Stop 又发它。
  4. **matcher 的比对对象是错误分类**：target 传分类值，于是 `matcher:
     "rate_limit|server_error"` 这种分流是有效的；退出码 2 与非 0 一律忽略——不注入上下文、
     不落 `hook.turn_blocked`、不叫模型再跑一轮，只留 `hook.completed` 审计（任务仍按
     API 错误失败收尾，钩子说什么都不改这个结论）。
  偏离：上游 `error_details` 是结构化错误对象，本机只有错误文本（前 500 字截断）；
  `last_assistant_message` 仅在图里已有助手回复时给（模型一次都没回上就没有这条）；
  `agent_transcript_path` 本批（#305）已补齐——触发点把检查点里的 `messages` 写成
  `.blank/context/agent-transcript.jsonl`，载荷带路径，钩子可读。
  判据 `services/api/tests/test_stop_failure_hooks_v55.py`（8 例：事件面与别名、21 格
  分类表、matcher 按错误分类分流、API 错误只发 StopFailure 不发 Stop、载荷带
  last_assistant_message、fire-and-forget 四点、续跑路径同样触发、无钩子零改动），
  变异 13/13 红（M1 摘注册 → 6 红；M2 摘别名 → 1 红；M3 塞进阻断集合 → 1 红；M4
  401 错译 → 5 红；M5 `model_timeout` 错译 → 1 红；M6 run 路径不接线 → 4 红；M7 续跑
  路径不接线 → 1 红；M8 多发一个 Stop → 2 红；M9 把 exit 2 当拦截 → 1 红；M10 把
  exit 2 当指令再叫模型 → 1 红；M11 target 传空 → 2 红；M12 不带 last_assistant_message
  → 2 红；M13 不带 error_details → 1 红；每条红集合均实测）；相邻钩子/子代理/设置套件
  **256 passed（92.42s）**，全量 **3560 passed / 28 skipped（1308.44s，RC=0）**。

- **settings.json 的钩子配置面与 matcher 语义（AC 批）**：钩子作者真正用的入口是
  settings.json 的 `hooks`，此前只有 `BLANK_HOOKS` 能配——等于"能写钩子，但没地方放"。
  上游锚点：`_zr(e,n,...)`（空 / `*` 全中、纯名单精确、其余正则）、`coi`（名单字符表
  `^[a-zA-Z0-9_|, -]+$`）、事件名到匹配目标的映射（PreToolUse→tool_name 等）、
  `disableAllHooks`（策略级总开关）。改 `services/hooks.py`、`services/settings.py`、
  `services/settings_layers.py`、`services/agent/runtime.py`。
  1. **两种形状都收**：Claude 嵌套 `{事件: [{matcher, hooks:[{type, command, timeout, async}]}]}`
     与既有扁平 `{event: [{match, command, ...}]}`；PascalCase 事件名经 `EVENT_ALIASES`
     折成内部蛇形名，未支持的事件告警后跳过（静默忽略会让用户以为配的钩子在跑）。
  2. **matcher 对齐 `_zr`**：空 / `*` / `.*` 全中；纯名单（`[A-Za-z0-9_|, -]`）按 `|` 与
     逗号拆分后精确比对；其余当未锚定的 Python 正则搜；非法正则配置期告警并丢弃。
     旧实现是 `self.match in target` 子串匹配——`Write|Edit` 会命中 `WriteFile`，
     `run_shell` 会命中 `run_shell_extra`，判据里三条反证就是它。
  3. **合并与总开关**：settings 钩子与 `BLANK_HOOKS` 合并，按「事件 + 命令 + matcher +
     超时」去重（同一条跑两次是 bug）；`disableAllHooks` 认驼峰与下划线两种写法
     （下划线优先），为真时一个都不加载。timeout 夹到 0.5–60 秒；非 `command` 类型、
     空命令、坏条目逐条告警跳过，告警只带事件与序号、不带命令正文（避免路径/参数进事件）。
  4. **安全段**：`hooks` 与 `disable_all_hooks` 进 `PROTECTED_SECTIONS`，项目/本机层
     写了进 `ignored`。理由：项目设置里的钩子 = 克隆一个仓库就执行它的任意命令；Claude
     靠工作区信任对话框挡这一步，我们还没有那道门。`disableAllHooks` 同理——项目层能关掉
     用户的守卫钩子也是提权。
  5. **接线与可观察**：`runtime._prepare` 从生效设置构建 `HookRunner`（不再只读 env），
     有钩子 / 有告警 / 总开关为真时落 `hooks.loaded`（每事件条数、来源 env|settings、
      disabled），不落命令正文。偏离：managed hooks 也受 `disableAllHooks` 约束（上游规定
     非管理层关闭不影响 managed），现结构按来源分不开；钩子类型只支持 `command`。
  判据 `services/api/tests/test_settings_hooks_v53.py`（20 例：两种形状、事件名映射、坏
  事件/类型/正则/空命令、timeout 夹取、合并去重、总开关、matcher 四种形态、层级 honored/
  ignored、别名优先级、settings 来源真子进程钩子的任务端到端 + 总开关反证），变异 9/9 红；
  相邻钩子/设置套件 209 + 135 例全绿，全量 **3543 passed / 28 skipped（786.84s）**。

- **钩子事件面第二批：图内权限钩子、通知、批处理、回合结束与压缩前后（AB 批）**：上一批
  只有事件名和网关侧接线，图形路径下真正发卡的地方是 `graph.py:_approval_gate` 里的
  `interrupt(...)`，网关的 ASK 分支在端到端里根本不会执行——所以 `PermissionRequest` /
  `Notification` 必须挂在图上，否则钩子作者写的东西在界面上永不触发。上游锚点：
  PreToolUse/PermissionRequest/PermissionDenied 三件套的 allow/deny 语义、Notification 按
  `notification_type` 匹配、PostToolBatch 载荷 `tool_calls`、Stop/SubagentStop 阻断把理由
  交回模型（第二次起 `stop_hook_active=true`，上限 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP ?? 8`）、
  PreCompact 输出拼进压缩提示词。改 `services/hooks.py`（事件全集、阻断集合、
  `STOP_HOOK_BLOCK_CAP`、stdin 带 `hook_event_name`）、`services/agent/graph.py`（图内
  权限钩子与通知、`AgentState` 声明）、`services/agent/runtime.py`、
  `services/agent/context.py`、`services/subagent.py`、`services/tools/gateway.py`。
  1. **PermissionRequest 接在发卡点**：放在 guardian 之后、`interrupt` 之前——审查员已经
     代批或拒掉的调用不会走到这里，也就不会让钩子去处理一张根本不存在的卡。allow 视同
     用户批准并记 `SCOPE_ONCE`（`approval.granted.rule=hook:permission_request`）；计划
     模式忽略 allow（那里的 ASK 是"这个动作不该发生"，不存在"钩子替我同意"）；deny 不弹卡，
     按 **call key** 把理由并进 `pre_tool_use` 的拒绝表（按工具名作键会查不中，模型看到的
     会是网关兜底的另一种文案），并记 `permission.denied` + 触发 `PermissionDenied`。
  2. **卡片出现前喊一声**：`Notification(notification_type=permission_prompt,
     title=需要审批：<tool>)` 在 `interrupt` 之前跑，桌面通知/IM 机器人只能在这一刻提醒人
     来看；通知钩子不阻断，失败也不影响发卡。子代理收尾发 `Notification(agent_completed)`。
  3. **PostToolBatch 一批一次**：逐条 `post_tool_use` 之外再发一次批事件，载荷
     `tool_calls[{tool_name, tool_input, ok, mode}]`，与单次事件不互斥。
  4. **Stop / SubagentStop 不许无限拦**：阻断后把理由交回模型继续，第二次起
     `stop_hook_active=true`，上限 `BLANK_STOP_HOOK_BLOCK_CAP`（默认 8）；`stop_blocked` /
     `stop_blocks` 必须是 `AgentState` 里**声明过**的键——langgraph 只合并状态里有的键，
     写在返回值里没声明会被静默丢弃，第二次读到的永远是"从没拦过"（首版实现真踩了，
     端到端实测 `stop_hook_active` 恒 false 后修正）。
  5. **PreCompact 是一次性的**：输出拼进压缩提示词且用完即清；`SessionStart` 按
     `source=startup`、`SessionEnd` 按 `reason=completed|waiting_approval|interrupted` 分派。
  6. 偏离/未移植：`UserPromptExpansion` / `TeammateIdle` 两个事件已由 AF 批移植，
     `session_crons`
     恒空、`background_tasks` 形状是 `{id, description, status}`、SubagentStop 的
     `agent_transcript_path` 已由 #305 批补齐（触发点把 `messages` 写成
     `.blank/context/subagent-transcript.jsonl`，载荷带路径）、Notification 只接
     `permission_prompt`/`agent_completed` 两类、
     SessionEnd 只有三种 reason。
  测试：新增 `tests/test_hook_events_v52.py` 24 例（事件注册与阻断集合、stdin
  `hook_event_name`、网关侧 allow/deny/计划模式/permission_denied/notification、**图内真实
  发卡路径**四个用例含 deny 跳过卡片与 allow 不弹卡、Stop 二次阻断与上限、路由、压缩、
  SessionEnd、子代理 stop，以及真子进程钩子的 post_tool_batch / stop / permission_request
  allow / permission_request deny / notification / SessionStart-End 端到端）。变异 9/9 红
  （阻断集合去掉 permission_request、stdin 去掉 `hook_event_name`、图内事件名改坏、计划
  模式认 allow、notification target 改坏、不合并 `hook_denied`、不清 `stop_blocked`、子代理
  不跑 stop 门、`agent_completed` 类型改坏）。相邻审批/钩子/子代理 12+11 个套件全绿；全量
  后端回归：**3523 passed / 28 skipped（781.43s）**。

- **Skill 的 `paths` 条件激活对齐上游 `conditionalSkills`（AA 批）**：frontmatter 里写
  `paths:` 的 Skill 不再只是"解析进 ignored"，而是按 Claude 的条件技能处理：清单里先
  不出现，等文件工具真碰到匹配路径后，在下一次模型调用前把"现在可以用了"作为系统提醒
  交给模型。上游锚点：归一化 `y1r`（字符串按逗号拆、`{}` 内不拆、列表拍平、非字符串
  丢弃、尾部 `/**` 去掉、空或全 `**` → 无条件）、文件操作后激活 `p1r`（
  `ignore().add(paths)` + `ignores(path)`，命中移入 `dynamicSkills`、记
  `activatedConditionalSkillNames`、log `Activated conditional skill '<name>' (matched
  path: <path>)`、emit `tengu_dynamic_skills_changed`）。改 `services/skills_defs.py`
  （`_path_list` / `_pattern_regex` / `match_skill_paths` / `SkillDefinition.paths|conditional`）、
  `services/tools/skills.py`（`SkillCatalog._visible|touch`、`touched_paths`、
  `activate_from_tool_results`、`render_skill_activation`、`preload_skills` 绕过条件闸门）、
  `services/agent/graph.py`（`ask` 注入提醒与 `skills.activated` 事件）。
  1. **只有真碰文件才算**：`read_file` / `edit_file` / `write_file` / `apply_patch` /
     `notebook_edit` 的成功回执才进台账；`glob` / `search_code` 的 pattern 是通配符不是
     路径，不能拿来猜激活；绝对路径与含 `..` 的段一律不匹配（与上游跳过一致）。
  2. **清单与工具两个面**：未激活的条件技能不进系统提示清单、`skill` 工具按名调用会被
     拒（报错里的「当前可用」也不泄漏名字）；只要目录里还有一个模型可调用的 Skill
     （哪怕它是条件的），`skill` 工具就不从工具表里拿掉——工具在、只是那一条还没解锁。
  3. **激活幂等且可复原**：`touch` 对已激活的项不再宣布；激活由工具台账反推，审批恢复
     重建会话目录后重放检查点里的结果即可复原，不依赖内存标记。
  4. **作者显式点名不受闸门拦**：子代理 frontmatter 的 `skills:` 预加载用
     `include_conditional=True`——这是作者写的派发指令，不是模型猜的名字。
  5. **同一轮就能解锁**：执行节点先跑工具再问模型，而工具结果要等节点返回才写回 state；
     本批把刚执行完的台账并进这次 `ask` 的视图，模型"读完文件紧接着调 skill"不再慢一步
     （首版实现里提醒会晚一轮注入，端到端实测暴露后修正）。
  6. 偏离/未移植：匹配器是 gitignore 近似（`*` / `?` 不跨 `/`、`**` 跨、尾缀 `/**` 去尾、
     `!pattern` 取反、无 `/` 的 pattern 任意层级），不是上游同款 `ignore` 库，罕见边界
     可能有出入；`synced` 同步根仍未移植。
  测试：新增 `tests/test_skill_paths_v51.py` 25 例（解析与 `ignored` 口径、`y1r` 形状、
  13 条匹配参数含取反与锚定、绝对与 `..` 拒绝、目录可见性与幂等、台账只认成功文件工具、
  工具闸门与报错不泄漏、子代理预加载绕过、提醒文案、五步真实运行时端到端含
  `skills.activated` 载荷）。变异 12/12 红（条件技能可见、touch 非幂等、条件技能算不进
  工具可用、`get` 绕过闸门、preload 被闸门拦、glob 计入触碰、失败回执计入触碰、尾缀
  `/**` 不去、pattern 一律锚定、取反失效、`ask` 用旧台账、提醒不注入）。相邻 12 个套件
  **347 passed**。全量后端回归：**3499 passed / 28 skipped（766.03s）**。

- **settings.json 的 `permissions.allow/deny/ask` 接进审批链（Z 批）**：Claude Code 的
  三组规则不再只是"看得到、不生效"。上游锚点：前缀规则 `zMe = /^(.+):\*$/`、glob
  转换 `aV`（大小写不敏感、空白折叠、`*` 跨空格）、matcher 分流 `NNt`
  （exact / prefix / wildcard）、`Bash` 命令拆段后前缀判定 `S===f || S.startsWith(f+" ")`、
  ask 规则压过 bypass。改 `services/permission_rules.py`（`parse_settings_rules` /
  `merge_rulesets` / `PermissionRule.mode`）、`services/permissions.py`（ask 分支、
  glob 匹配、`Decision.tool_scope_allowed`）、`services/settings.py`（`defaultMode`
  别名与三组列表归一）、`schemas/settings.py`、`services/agent/runtime.py`（加载与
  事件）、`services/agent/graph.py` 与 `services/tools/gateway.py`（发卡接线）。
  1. **两种口径**：`Bash(cmd:*)` 是前缀（token 边界，与上游 `startsWith(f+" ")` 等价），
     `Bash(cmd)` 是整条子命令的 glob，`Bash(*)` 是整工具；规则带 `mode` 并进指纹，
     换一种读法就是另一版规则。
  2. **命令规则逐段判定**：allow 要求每一段子命令（含 `cmd /c "…"` 的内层载荷）都被
     覆盖；deny/ask 任一段命中就成立，`echo hi && rm -rf src` 不会搭便车。
  3. **失败方向**：写不出来的条目 allow 记 note 跳过（少放行＝安全）；deny/ask 写不出来
     （MCP 工具、`domain:` 规则、工作区外路径、带 `*` 的 `:*` 前缀、结构错误）则整份
     settings 规则停用并进 `error`——一份看起来防住了却没生效的 deny 比没有更糟。
     文本规则文件仍是坏一行即整份停用。
  4. **优先级**：deny > 计划模式闸门 > ask > host_bypass / irreversible > bypass /
     `approve_high_risk` > 会话驳回 / 会话批准 > 前缀账 > allow > 工具档 > 接受编辑。
     ask 命中时卡片不给「本会话总是允许」（下一次照样命中，按了不兑现）；三个发卡点
     按 `tool_scope_allowed` 收掉该档。
  5. **`defaultMode` 认 Claude 名字**：`default` / `acceptEdits` / `plan` /
     `bypassPermissions`（下划线写法优先）。原实现直接 `lower()` 得到
     `acceptedits` / `bypasspermissions`，静默回落 `default`——设置里写了「全放行」，
     运行时仍在逐条问；本批一并修掉。
  6. **来源可见**：settings 规则落 `permission_rules.settings`（含 `source` 层、
     `notes`、错误）；与文本文件合并后的生效集再落 `permission_rules.loaded`，
     文件里的规则先命中。
  7. 未移植/偏离：项目层与本机层的 `permissions` 仍受安全段保护被忽略（克隆仓库不能
     替用户放宽审批）；`mcp__server__tool` 与 `WebFetch(domain:)` 规则不接入（allow 记
     note、deny/ask 整份停用）；子代理策略与 #112 一致不带 ruleset。
  测试：新增 `tests/test_settings_permissions_v50.py` 34 例（工具名表与真实注册表逐名
  对齐、前缀 vs 整串、逐段 allow/任一段 deny、路径闸门、写不出条目的两个方向、归并与
  sha、`defaultMode` 别名、SettingsStore 往返、判定优先级；真实运行时三条端到端：
  deny 命令没跑、allow 免卡、ask 仍挂卡且卡片只有「仅本次」）。变异 10/10 红（ask
  分支置死、计划闸门改 ask、deny 写不出降级成 note、裸 Bash 当前缀、路径闸门关、
  glob allow 改任一段、`defaultMode` 别名丢、ask 保留工具档、运行时不合并 settings
  规则、graph 发卡恒给工具档）。另测 gateway 发卡点：本文件的三条端到端不经过该分支，
  变异存活（不是 no-op——`tool_grant_too_wide` 之外的路径仍需要它），与 graph 同形，
  列为未验证项。相邻 11 个套件 **214 passed**。全量后端回归：**3474 passed /
  28 skipped，729.12s，rc=0**（本轮同时修掉 `test_validation_approval_identity_v101.py`
  的审批桩：`Decision` 新增 `tool_scope_allowed` 后桩未同步，7 条走 `graph.py` 发卡点的
  用例报 `AttributeError`）。

- **子代理定义的 `permissionMode` / `isolation` / `background` 对齐上游（Y 批）**：
  文件型与插件 agent 的这三个字段不再当未支持字段。上游锚点：合法档位 `fw` 与
  `manual`→`default` 别名 `Vf`、严格度 `Ho` 与「只能收紧」判定 `lK`
  （`vs=lK($t,kr); bs=vs??e.permissionMode`）、隔离生效值 `Szn`（派发参数 ?? 定义
  值）、后台判定 `Pts`（`background:true` 压过调用方 false）、插件 agent 解析
  `Skn`（名字/描述回退 + 三类控制告警忽略）。改 `services/agents_defs.py`、
  `services/subagent.py`、`services/tools/delegate.py`。
  1. **只收紧不放宽**：本机审批档序列 plan < default < accept_edits < bypass；派发
     基线隔离副本 accept_edits、非隔离 default。声明档序号不大于基线才生效，更宽的
     声明忽略并落 `permission.mode.ignored`（reason=`looser`）。上游允许从 default
     父档升到 acceptEdits；本机隔离副本的基线本身就是 accept_edits（副本要能写），
     所以定义只能在它之上继续收紧——这是有意偏离，见 README。
  2. **`auto` / `dontAsk` 只识别不落地**：上游合法，本机没有对应审批档；解析时告警
     并收进 `agent_ignored`，不猜一个近似档。
  3. **可观察**：生效/忽略写 `permission.mode.applied|ignored`（declared / baseline /
     effective / reason / fork），`subagent.started` 带 `agent_permission_mode`；
     fork 的权限对象就是父会话那一份，声明一律 `fork_inherits_parent`。
  4. **worktree 强制隔离**：`isolation: worktree` 压过调用方 `isolate:false`；
     `remote` 未移植，按本地运行并告警；插件 agent 只认 worktree，其余静默丢弃。
  5. **background 强制后台**：只认显式 true（布尔或字符串），会压过
     `run_in_background:false`；`false` 等同没写；续聊保留 `permission_mode`。
  6. **插件回退**：名字缺省用文件名（去掉 `.md`），描述 `description` → `when_to_use`
     → `when-to-use` → `Agent from <plugin> plugin`；`permissionMode` / `hooks` /
     `mcpServers` 逐条告警忽略（与上游 `Skn` 一致，真正的控制请放 `.blank/agents/`）。
  测试：`tests/test_agent_defs_v44.py` 45→62 例（档位解析/`manual` 别名/非法值与
  `auto`·`dontAsk`、隔离与后台解析、审计视图、收紧表、真跑对照：同一次隔离派发
  `plan` 拒写而基线放行、续聊两轮仍 plan、工具层 worktree/background 强制、并行批次
  同样生效）；`tests/test_plugins_v49.py` 26→30 例（名字/描述回退、三类控制告警、
  worktree 生效与非法静默）。Y 批变异 13/13 红（丢严格度比较、`run_subagent` 忽略
  声明、started 事件不报生效档、续聊丢声明、worktree 不强制隔离、background 不强制
  后台、丢 `manual` 别名、`remote` 当生效值、插件名字回退、描述回退、忽略循环、非法
  隔离告警、worktree 解析）。相邻套件（fork / 子代理默认后台 / 子代理 / 子代理用量 /
  续聊 / 插件 / Skill / 命令 / skill 工具 / 提案）184 passed；扩到边界/工作树/证据
  20 个套件 **384 passed**。全量后端回归：**3440 passed, 28 skipped, rc=0**（730.11s）。

- **子代理 frontmatter 的 `skills` 预加载对齐上游 `OI` + `formatSkillLoadingMetadata`（X 批）**：
  文件型与插件 agent 的 `skills` 不再当未支持字段，派发前把声明 Skill 的正文预加载成一条
  meta user 消息。上游锚点：过滤循环 @15191346（`(e.skills??[]).filter((Wi)=>!Ya.has(Wi))`
  与五类 warning）、预加载消息拼装 @15191925（`formatSkillLoadingMetadata` + 正文、
  `isMeta:!0`）、续聊 carriedPreloads @15188674 / `kDs` @15205370（用
  `isSkillLoadingMetadata` 扫 meta user 消息）。改 `services/agents_defs.py`（`skills`
  字段与 `OI` 解析）、`services/subagent.py`（预加载消息、carried 跳过、续聊保留）、
  `services/tools/skills.py`（`skill_loading_header`、`preload_skills`）、
  `services/tools/delegate.py`（定义透传）。
  1. **解析口径同 `OI`**：缺省与显式 null 都是空表；`*` 保留成字面名字（可能找不到、
     按缺名告警），不按工具通配符展开；分词复用 `fyn` + `Wd`（逗号与空格都切、括号内
     保持一条、非字符串项丢弃并告警，告警文案按 Skill 措辞）。
  2. **缺名只告警不阻断**：`Skill '<x>' specified in frontmatter was not found` 进
     `skill.preload.failed` 事件，派发继续；成功进 `skill.preloaded`（名字 + 字数），
     `subagent.started` 带 `agent_skills`。
  3. **预加载消息排在 goal 之前**：`<command-message>` / `<command-name>` /
     `<skill-format>` 三段头 + 正文作为 user 消息；续聊先扫历史里的同一个头，已经进过
     上下文的不再重复贴（对应 `kDs` 的 carriedPreloads）。
  4. **预加载即已加载**：名字记进 `catalog.loaded`，子代理再调 `skill` 工具返回
     「已加载」，不会把正文贴第二遍（上游 `ykr`）。
  5. 未移植：`syncedSkills`（账户同步）与「非 prompt 型」「策略禁用」两类上游 warning
     分支——本机 Skill 目录只装载 prompt 型，也没有账户同步链路，没有对应状态可判；
     远程同步仍整条冻结。
  测试：`tests/test_agent_defs_v44.py` 新增 9 例（逗号串/列表、缺省与 null 都为空、
  坏类型 fail-closed 且用 Skill 措辞、`*` 是字面名、审计视图收进 skills 不再报
  unsupported、预加载排在 goal 前且父会话 loaded 不变、缺名仍派发、续聊不重复、
  预加载后 skill 工具返回已加载），全套 45 passed。变异 6/6 红（`*` 按通配符归一、
  整段跳过解析、不记 loaded、carried 恒空、预加载排到 goal 之后、缺名静默跳过）。
  相邻套件（fork / 子代理默认后台 / 子代理 / 子代理用量 / 插件 / Skill / 命令 /
  skill 工具 / 提案）232 passed。全量后端回归：**3419 passed, 28 skipped**
  （704.58s）。

- **子代理定义的 `tools` / `disallowedTools` 对齐上游 `VW`（W 批）**：文件型与插件
  agent 的这两个字段都走 `fyn` + `Wd` 再包一层 `VW`（`fyn` @19523546、`Wd`
  @43262594、`VW` @19523799；调用点 @5785886 插件 agent、@20358524 文件型 agent）。
  改 `services/agents_defs.py`：删掉本地「只切逗号」的实现，复用
  `skills_defs._tool_list`，并用 `_MISSING` 区分「字段缺省」与「显式 null」。
  1. **缺省 ≠ null**：缺省走 `undefined`（不限），显式 `null` / 假值走 `[]`
     （没有工具）。此前把两者都当 `None`，等于把用户的 `tools:` 空行读成最大授权。
  2. **通配符按 `VW` 归一**：列表里任何 `*` 先由 `fyn` 归一成 `["*"]`，`VW` 再转成
     `undefined`——`tools: "*"` 与缺省同义，`disallowedTools` 里的 `*` 则整条省略。
     此前我们把禁止列表的 `*` 按「全部禁用」处理，方向与上游相反。
  3. **分词口径与 Skill/命令共用**：空格与逗号都切、括号内保持一条、数组里的
     非字符串项丢弃并进审计告警（上游静默 filter；告警不改变结果）。
  测试：`tests/test_agent_defs_v44.py` 新增 5 例（空格切分保括号、空行 null 与缺省
  分开、列表内 `*` 归一、假值=没有工具、禁止列表 `*` 整条省略），替换 1 例钉旧口径的
  用例；36 passed。变异 5/5 红（只按逗号切、通配符不归一、null 当缺省、假值走错误
  分支、禁止列表不归一成空元组）。相邻套件（fork / 子代理默认后台 / 插件 /
  Skill / 命令 / skill 工具）195 passed。全量后端回归：**3410 passed, 28 skipped**
  （rc=0，728.61s）。

- **工具规格解析对齐上游 `fyn` + `Wd`（V 批）**：`allowed-tools` /
  `disallowed-tools` 从「只按逗号切」改成 Claude 的口径。核对
  `E:/env/rea/out/claude.uniq.txt` 的 `fyn`（@19523546）与分词器 `Wd`
  （@43262594），以及 8 处调用点（@4662204、@5331371、@5331545、@16103864、
  @19531446、@20805013、@14427699、@34939794）：`fyn` 对 null 返回空表；字符串整体
  交给 `Wd` 分词；数组只保留字符串项、非字符串项丢弃而不是把整字段判无效；分词时空格
  与逗号都是分隔符，但括号内不切（`Bash(git add, git commit)` 保持一条），每段 trim
  后丢空段；列表里出现 `*` 时整体归一成 `["*"]`（上游的「全部工具」）。改
  `services/skills_defs.py`（新增 `_split_tool_specs`、重写 `_tool_list`）；
  `services/commands_defs.py` 复用同一函数，Skill 与斜杠命令两侧同时生效。
  1. **只改解析、不改工具池**：解析结果仍只进审计面，工具池由网关与场景决定
     （与 U 批同一边界）。
  2. **数组非字符串项丢弃并告警**：上游是 `filter`，这里同样保留字符串项，只对
     「丢了东西」发提示，不再把整字段判成无效。
  3. **`*` 在占位符替换之前归一**：先出 `("*",)` 再做 `${CLAUDE_SKILL_DIR}` /
     `${CLAUDE_PROJECT_DIR}` 替换，与上游 `fyn` 先于 `Iot` 的顺序一致。
  测试：`tests/test_skills_disk_v45.py` 新增 4 例（空格切分保括号、数组丢非字符串
  项、`*` 归一、假值不告警），`tests/test_commands_v46.py` 新增 1 例（命令侧走同一
  分词器），两套件 82 passed；变异 5/5 红（只按逗号切、去掉括号内不切、数组遇非字符串
  整字段作废、去掉 `*` 归一、假值仍告警），前两条同时命中命令侧的共享分词用例；
  相邻套件（`test_skill_tool_v33`、`test_plugins_v49`、`test_ask_goal_skills_v43`
  连同上述两个）189 passed。全量后端回归：**3406 passed, 28 skipped**
  （rc=0，709.01s）。

- **Skill `allowed-tools` 占位符替换（U 批）**：按上游原文补齐
  `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}` 的替换语义。核对
  `E:/tmp/rea-claude/skill_parse_fragment.txt` 里的 `Iot(...)`：只对 `allowedTools`
  做替换，Skill 目录先换、工作区根后换，路径统一 `\` → `/`；`disallowed-tools`
  原样保留。改 `services/skills_defs.py`（新增 `_tool_placeholders`、
  `_definition_from_dir(project_root=...)`、`discover_skills` 与 `load_skill` 透传
  工作区根）。
  1. **只动 allowed-tools**：替换发生在 `_tool_list` 解析成功之后；工具池仍由网关与
     场景决定，替换出来的只是审计面里的完整路径。
  2. **用户层也用当前工作区**：`${CLAUDE_PROJECT_DIR}` 取的是运行时根，不是
     `~/.blank`；用户层与项目层、插件层同一口径。
  3. **正斜杠**：Windows 反斜杠在工具名/规格里会被当转义，替换后统一 `/`（与上游
     `qYe` 的 `replaceAll("\\", "/")` 同一口径）。
  4. 仍未移植：`${CLAUDE_EFFORT}` 在当前上游转储里没有出现，按未验证处理；自定义斜杠命令的
     `allowed-tools` 不替换——转储里能看到插件命令把工具表交给 `lme`，但看不到 `lme`
     是否与 Skill 共用 `Iot`，没有证据就不动。
  测试：`tests/test_skills_disk_v45.py` 新增 2 例（两占位符同时替换且 disallowed
  不动、用户层把项目根解析到工作区），全套 35 passed；变异 4/4 红（撤掉替换调用、
  去掉 Skill 目录的斜杠归一化、去掉项目根的斜杠归一化、把替换也套到 disallowed）；
  相邻套件（`test_skill_tool_v33`、`test_plugins_v49`、`test_ask_goal_skills_v43`、
  `test_skills_disk_v45`）142 passed。全量后端回归：**3401 passed, 28 skipped**
  （rc=0，710.27s）。首次全量出现 1 例
  `test_real_file_e2e_v153::test_real_file_loop_read_break_test_fix_pass` 落 failed；
  该用例只走 read_file / apply_patch / run_tests，与本批改动无调用交集，单文件 5 连跑
  10/10 绿、整轮复跑全绿，按全量负载下的偶发记录——没有调大超时、也没有改判据。

- **fork 子代理（Agent(fork)）与链式编辑口径修正（T 批）**：`spawn_agent` 新增内置类型
  `fork`，语义对齐 Claude 的会话分身：继承派发这一刻的完整对话与系统提示、复用父会话的
  `PermissionPolicy` 与工具表，只换一个新的上下文窗口。改 `services/agents_defs.py`
  （内置 `fork` 定义）、`services/subagent.py`（`SubAgentRequest.fork`、`_fork_tools`、
  fork 系统提示、权限与后台 shell 继承、无快照 fail-closed、续聊保留 fork）、
  `services/tools/delegate.py`（类型解析、`spawn_agents` 与显式后台的拒绝）、
  `services/agent/graph.py`（`AgentSession.conversation_snapshot` 与
  `_fork_history`，在 `execute_action_node` 执行这批工具之前刷新）。
  1. **快照是派发那一刻**：图节点一开始就取 `state["messages"]` 的快照，并剪掉最后一条
     尚未拿到回执的 `assistant(tool_calls)`——否则分身会看到「assistant(tool_calls) 直接
     跟 user」的非法消息序列。
  2. **工具面与权限是继承不是新授**：工具取父网关当前的工具表，减去派发类
     （`spawn_agent(s)` / `send_message` / `task_*` / `list_agents`）与 REPL/LSP/workflow，
     并剪掉父网关的场景级禁用；子网关沿用父网关的场景（文件类型白名单）与 `write_root`，
     不能靠重建一个默认 dev 网关把「文档场景只能写 .md」或逆向写根在分身里打开。
     权限复用父会话的同一个 `PermissionPolicy` 对象，审批卡在子代理里弹不出来，需要
     审批的动作照旧被拒绝。
  3. **只能前台**：fork 显式传 `run_in_background=true` 是参数错，直接拒绝；
     `spawn_agents` 并行批同样整批拒绝（并行批里的分身各自拿到同一份快照，结论会落在
     父会话走远之后）。
  4. **拿不到父对话就失败**：没有快照时返回「fork 子代理拿不到父会话对话」，不伪装成
     普通只读子代理；`subagent.started/completed` 事件带 `fork` 标记。
  5. **链式编辑口径**：核对上游原文（`E:/tmp/rea7/claude_tools_dump.txt` 第 230–231 行）
     后确认 Edit 只要求「本会话内读过、`old_string` 精确且唯一」，没有「链式编辑必须
     拒绝」这条规则；README/CHANGELOG 的旧表述按实修正，并补三跳链式编辑判据
     `test_three_chained_edits_after_one_read`。
  6. **remote 形态冻结**：Qoder 文档只有 `kind` 字段，没有可验证的 remote 协议/端点；
     没有对端就写不出能失败的判据。重开条件：拿到可复现的 remote 端点（含鉴权与版本
     协商）并本机跑通一次派发。在此之前按未知类型拒绝。
  测试：新增 `tests/test_claude_fork_v30.py` 15 例（内置类型、对话与系统提示继承、
  未回执交换剪裁、图节点派发前刷新、工具面收窄、权限对象共享、无快照 fail-closed、
  前台默认与显式后台拒绝、并行批拒绝、续聊保留 fork、场景禁用剪裁、文件类型闸门与
  写根继承），变异 11/11 红（前 8 格之外补：不剪场景禁用、重建网关丢场景闸门与写根、
  不把父网关接进 fork；跑完逐条还原并核对 sha256）。相邻套件
  （`test_claude_subagent_default_v29`、`test_background_subagent_v116`、
  `test_agent_continuation_v25`、`test_agent_defs_v44`、`test_claude_parity_v24`、
  `test_claude_fork_v30`、`test_tool_search_v13`）合计 153 passed。
  另修 `spawn_agent` 描述中 fork 段落的措辞（「并行批次」→「多任务批次」）：单代理
  工具描述里不该出现检索别名词「并行」，否则 `tool_search("并行")` 会把
  `spawn_agent` 与 `spawn_agents` 打成同分并按注册顺序反超。
  全量后端回归：**3399 passed, 28 skipped**（0 failed，rc=0，728.32s）。

- **MCP 配置三作用域与项目层批准（`.mcp.json`）对齐 Claude 的 user < project < local 级联（S 批第 8 项）**：
  按 Claude Code 2.1.271 的作用域口径落成本地实现。新增
  `services/mcp/approvals.py`（批准记录的读写、整条定义摘要、工作区绑定、原子写），改
  `services/mcp/bridge.py`（三层路径、整条覆盖合并、批准闸门、按工作区分桶的
  `MCPManager`）、`services/mcp/client.py`（`ServerConfig.scope`）、
  `api/routes/settings.py`（状态端点按项目读、批准/撤销端点）、
  `services/agent/runtime.py`（用原始工作区根取配置）、
  `web/src/components/settings/{api.ts,mcp-panel.tsx}`（项目下拉、作用域/批准徽标、批准与撤销）。
  1. **层与路径**：用户层 `mcp.json`（`BLANK_MCP_FILE` 或设置目录旁）、项目层
     `<workspace>/.mcp.json`、本机层 `<workspace>/.blank/mcp.local.json`，从低到高读取；
     同名服务端**整条覆盖**、字段不跨层合并——高优先级层写 `disabled` 就是把同名服务端
     整条关掉，而不是只改一个字段。坏 JSON / 顶层非对象只给这一层一条 `failed` 状态，
     不拖垮其余层。
  2. **批准闸门**：项目层与本机层的条目在批准前**不启动进程**（状态 `skipped` +
     `approval=pending`，原因写明「批准这条定义之前不会启动进程」）；用户层沿用原有
     免批准行为。批准记录按「作用域 + 工作区路径哈希 + 服务端名 → 整条定义的 sha256」
     存 `mcp-approvals.json`（`BLANK_MCP_APPROVALS_FILE` 可改），只存哈希、不复制
     `env` / `headers`（密钥不会多一份副本）；改一个字节或换一个工作区都会退回
     `pending`；坏批准文件按「什么都没批准」fail-closed。
  3. **缓存与重连**：`MCPManager` 按工作区分桶缓存（key 是工作区路径哈希），
     stamp 覆盖用户/项目/本机三份配置与批准文件，任一改动即重连；批准/撤销后即时生效，
     不用重启。运行时取配置用原始工作区根，而不是任务的隔离副本。
  4. **可观察**：`GET /settings/mcp?project_id=` 返回每条的 `scope` / `approval`、
     三层路径与是否存在；`POST /settings/mcp/approvals` 与
     `DELETE /settings/mcp/approvals/{name}?scope=&project_id=` 批准/撤销并立即重连，
     批准用户层、批准不存在的名字、批准被更高层覆盖的定义分别给 400 / 404 / 409。
     设置页 MCP 面板加项目下拉、作用域徽标、待批准/已批准与批准/撤销按钮，空状态摊开
     三层路径。
  测试：新增 `tests/test_mcp_scopes_v50.py` 25 例（三层覆盖与整条取代、禁用遮蔽、
  未批准不启动、批准后连接、改定义/换工作区退回 pending、批准文件不含密钥、摘要键序
  无关值敏感、坏批准文件 fail-closed、撤销即时、拒绝批准用户层、坏层隔离、分桶缓存、
  HTTP 端点 400/404/409、运行时装配），变异电池 17/17 红（逐条注入：层序反转、不读
  批准、批准不绑定工作区、整条覆盖变字段合并、stamp 不含批准文件、端点不校验作用域等；
  跑完按原字节还原并核对 sha256）。
  全量后端回归：**3383 passed, 28 skipped**（0 failed，rc=0，720.83s）。
  浏览器点验（隔离库 :8171 + Next dev :3171 + headless Chrome，受控实例关掉限流）：
  待批准 → 点批准 → 已连接/4 工具 → 切「仅用户层」→ 切回仍已批准 → 撤销 → 退回待批准，
  基线 14/14 绿、还原后复跑 14/14 绿、无控制台错误，装置自身 rc=0；两条源码变异分别
  只红在依赖它的判据上（作用域徽标缩写 → 恰红 B1；撤销分支改成再次批准 → 恰红
  E1/E2/E3）。前端 `tsc --noEmit` 与 `eslint src` 均 rc=0。
  偏离：项目层用 `.mcp.json`（与 Claude 一致），本机层放 `<workspace>/.blank/mcp.local.json`
  而不是 `~/.claude.json` 的项目键——项目私有配置随工作区目录走，拷走工作区不会带走批准；
  批准记录单独存 `mcp-approvals.json` 且只存哈希，不像 Claude 把批准状态连同原始配置
  写进项目私有段；没有 `/mcp` 交互命令与 OAuth 浏览器授权流程，批准只走设置页与 HTTP
  端点；没有服务端级工具 allow/deny 过滤（Claude 能逐工具审批与禁用），本机只按整条
  定义批准或撤销。

- **插件包（`.claude-plugin/plugin.json`）对齐 Claude 插件契约（S 批第 7 项）**：
  按 Claude Code 2.1.271 的插件包口径落成本地实现。新增
  `services/plugins_defs.py`（发现、清单校验、组件路径解析、启用解析），改
  `services/commands_defs.py` / `skills_defs.py` / `agents_defs.py` /
  `output_styles_defs.py`（四类组件按 `<plugin>:<name>` 命名空间接入）、
  `services/settings.py` / `settings_layers.py`（`plugins.enabled` 段与顶层
  `enabledPlugins` 合并）、`api/routes/projects.py`（`GET /projects/{id}/plugins`）、
  `web/src/components/settings/{api.ts,plugins-panel.tsx,settings-view.tsx}`（插件面板）。
  1. **发现**：用户层 `~/.blank/plugins/<name>/.claude-plugin/plugin.json`
     （`BLANK_USER_PLUGINS_DIR` / `BLANK_USER_INSTRUCTIONS_DIR/plugins` 可改）+
     项目层 `<workspace>/.blank/plugins/...`；同名项目层胜并告警；缺清单的目录进
     告警；坏清单（坏 JSON / 顶层非对象 / 缺 name / 名字以 `-` 开头或含 `:` /
     超 1 MiB）只跳过该插件并点名，其余插件照常加载。
  2. **组件与命名空间**：`commands` / `agents` / `skills` / `outputStyles` 四类
     提示词组件接进既有加载器，名字按 `<plugin>:<name>`（`/deploy-tools:review`
     可直接展开；裸名字不能命中插件组件）；清单路径覆盖替换默认目录，必须以 `./`
     开头且不得逃出插件目录，违规告警后回退默认目录或跳过该项。
  3. **启用口径**：`enabledPlugins`（顶层）或 `plugins.enabled`（同层后者优先，
     跨层仍按 用户 < 项目 < 本机 < 管理 叠加）；键可为 `name` 或
     `name@marketplace`（按 `@` 前基名匹配）；没写时取清单 `defaultEnabled`
     （默认 true）；停用插件不注入任何组件；设置里指到本机不存在的插件给告警。
  4. **只报告、不加载**：`hooks` / `mcpServers` / `lspServers` 与自带 `.mcp.json`
     只进 `ignored`（带原因），不执行、不连接——克隆仓库里的插件包不能换来进程
     执行或外连。
  5. **可观察**：`GET /projects/{id}/plugins` 返回清单、组件计数、启用状态、
     ignored 与告警（插件条目路径打码、项目层只给工作区相对路径）；设置页「外部
     工具与插件」新增插件面板：清单模板、命名空间说明、停用原因、忽略段与告警、
     「重新扫描」；项目下拉默认选第一个绑了工作区的项目。
  测试：新增 `tests/test_plugins_v49.py` 26 例（发现与清单字段、路径覆盖与两级校验、
  四类组件命名空间、启用三口径、设置合并与归一化、真实 HTTP 列表），变异电池
  15/15 红（四处前缀丢失、停用仍注入、忽略 defaultEnabled、顶层开关不合并、
  `./` 校验删除、逃逸校验删除、hooks 不报告、层级反了、坏清单带崩目录、`@`
  后缀不剥、接口泄漏绝对路径、normalize 优先级反转；跑完按原字节还原并核对
  sha256）。
  全量后端回归：**3358 passed, 28 skipped**（0 failed，rc=0，777.53s）；前端
  `tsc --noEmit` 与 `eslint src` 均 rc=0。
  偏离：目录用 `.blank/plugins` 而不是 `.claude/plugins`，用户层是
  `~/.blank/plugins`，且刻意只读 `.blank`；没有 marketplace（网络安装 / 更新源），
  只认本地目录，`name@marketplace` 键只按基名匹配；没有 `/plugin` 交互命令与启用
  写回；`hooks` / `mcpServers` / `lspServers` 与自带 `.mcp.json` 只报告不加载
  （刻意的安全边界）；清单里的 `force-for-plugin` 沿用 S4 口径只解析、不生效。


- **MCP 配置变量展开（`mcp.json`）对齐 Claude `.mcp.json`（S 批第 6 项）**：
  按 Claude Code 2.1.271 的 `.mcp.json` 展开口径落成本地实现。改
  `services/mcp/bridge.py`（重写 `_expand`、`parse_servers` 注入 env 与逐服务端告警、
  `MCPStatus.warnings`）、`services/mcp/client.py`（`ServerConfig.warnings`）、
  `web/src/components/settings/api.ts` 与 `mcp-panel.tsx`（状态页逐条 ⚠ 展示）。
  1. **语法**：`${VAR}` 与 `${VAR:-default}`；变量名必须是
     `[A-Za-z_][A-Za-z0-9_]*`；同一字符串里的多个占位符逐个替换、重复变量只告警一次；
     未闭合的 `${...` 原样保留（不猜）。
  2. **取值口径**：变量已设（含设成空串）→ 取环境值；`${VAR:-default}` 在变量未设
     **或为空**时用默认值（bash `:-` 语义）；未设且没有默认值 → **保留原文**并告警。
     这里刻意偏离 shell 的空串替换：替成空串会把「忘了配」变成「配了个空 token」，
     症状要到服务端 401 才显形。默认值按字面使用、不再递归展开。
  3. **作用域**：`command` / `args`（逐元素）/ `env`（值）/ `url` / `headers`（值）五处
     ——与 Claude 一致；本机另把 `cwd` 与 `auth` 段一并展开。`settings.json` 本身不展开
     （与 Claude 一致，见上一条的偏离修正）。
  4. **告警不泄值**：告警只说变量名；非法变量名连占位符原文都不回显
     （`${sk-live-...}` 这类疑似密钥不进日志），只有「变量名不合法，已原样保留」一句。
  5. **可观察**：告警按服务端去重，随 `MCPStatus.warnings` 出现在 `GET /settings/mcp`；
     连上的、连接失败的、被跳过的服务端都带；设置页的 MCP 面板逐条以 ⚠ 显示，
     不用等到工具调用失败才发现变量没配。
  测试：`tests/test_mcp_tools.py` 新增 11 例（五字段展开、默认值两态、未设保留原文 +
  告警、空串两态、多占位符与告警去重、非法变量名不回显、默认值不递归、未闭合占位符、
  被跳过服务端带告警、状态端点告警），变异电池 10/10 红（逐条注入：替空串、不记告警、
  默认值抢先生效、空串不回退默认、空串当未设、回显 spec、告警丢失在两处接线、
  二次展开、告警不去重；跑完按原字节还原）。
  全量后端回归：**3332 passed, 28 skipped**（0 failed，rc=0，718.84s）。
  偏离：配置文件是设置目录旁的一份 `mcp.json`，没有 Claude 的项目 `.mcp.json` 与
  user/project/local 三作用域（整条服务端取最高来源、字段不跨层合并）那套——三作用域
  与项目层批准已在 S8 补齐（见本批第 8 项）；展开面多了
  `cwd` 与 `auth` 段；`${VAR:-default}` 的「空串算未设」按 bash 语义实现——上游二进制里
  找不到这段实现，按官方文档口径（未设告警并保留原文；作用于五处）解释。

- **设置层级（用户 < 项目 < 本机 < 管理策略）对齐 Claude settings 级联（S 批第 5 项）**：
  按 Claude Code 2.1.271 的 settings 级联口径落成本地四层叠加。新增
  `services/settings_layers.py`（层路径规划、单层读取、键级合并、安全段保护、审计投影），
  改 `services/settings.py`（`SettingsStore.read_layered`）、
  `services/agent/runtime.py`（每任务在运行根上叠一次、`settings.layers` 事件）、
  `api/routes/projects.py`（`GET /projects/{id}/settings`；`output-styles` 的 selected 改读
  级联）、`web/src/components/settings/api.ts`（类型）。
  1. **层与路径**：用户层 `BLANK_SETTINGS_FILE`（桌面壳指到用户数据目录，缺省沿用
     `settings_path()`）+ 项目层 `<workspace>/.blank/settings.json` + 本机层
     `<workspace>/.blank/settings.local.json` + 管理层 `BLANK_MANAGED_SETTINGS_FILE`
     （不配就没有这一层）。没有工作区时只剩用户层与管理层，结果与旧的 `read()` 一致
     （`read_layered(None)` 与 `read()` 逐键相等的判据钉住）。
  2. **叠加**：从低到高 user → project → local → managed；高一层只覆盖它写了的键，嵌套
     对象逐键合并，数组与标量整段替换；合并后仍走 `normalize()`，夹取与回落语义跟用户层
     完全一致；未知设置段进该层 `ignored`（带原因），不参与合并。
  3. **安全段保护（本机偏离）**：`guardian` / `permissions` / `sandbox` / `safety` /
     `features` 只认用户层与管理层；项目/本机文件写了也进 `ignored`（「安全设置只能由
     用户层或管理策略决定」），有效值不变——克隆来的仓库不能靠提交 `settings.json` 替
     用户放宽审批或安全边界。被挡下的段在审计事件里逐段留痕，不静默吞掉。
  4. **坏层隔离**：坏 JSON（点名第几行）/ 顶层不是对象 / 超 1 MiB 只跳过这一层并进
     `errors`，其余层照常叠加；缺层不算错误。读设置失败整任务起不来，比少一层偏好更糟。
  5. **可观察**：层、来源与 ignored 落 `settings.layers` 事件（只在用户层之外有东西或有
     坏层时写），路径经 `mask_path` 打码、不含任何设置值；`GET /projects/{id}/settings`
     返回生效值 + `layers` / `sources` / `active` / `warnings` / `errors` + 打码后的用户层
     路径；`GET /projects/{id}/output-styles` 的 `selected/active` 也改用工作区级联。
  测试：新增 `tests/test_settings_layers_v48.py` 28 例（四层顺序与来源、local>project>user
  三阶段重写、嵌套合并与数组替换、无工作区/无层回落、安全段五段矩阵、管理层可钉安全段、
  未知段、坏 JSON/顶层数组/超 1 MiB 隔离、缺层、normalize 夹取、环境变量开关、审计打码
  且不泄值、`read_layered(None)` 等价、运行时系统提示与事件、项目设置端点、output-styles
  级联），变异电池 27/27 红（逐条改坏实现，红在指定判据上；跑完按原字节还原并核对
  sha256）。
  全量后端回归：**3321 passed, 28 skipped**（0 failed，rc=0，717.79s）。
  偏离：目录用 `.blank/settings.json` / `.blank/settings.local.json` 而不是
  `.claude/...`；安全段只认用户层与管理层（Claude 允许项目/本机层改
  `permissions.defaultMode`）；`settings.json` 不展开 `${VAR}`（与 Claude 一致；
  `mcp.json` 的五字段展开已在 S 批第 6 项补齐）；管理层只认
  `BLANK_MANAGED_SETTINGS_FILE`，没有平台默认管理路径与 `policySettings` 那套；设置页
  仍只读写用户层，叠加结果走 `GET /projects/{id}/settings` 只读展示。

- **输出样式（`.blank/output-styles`）与系统提示接入对齐 Claude（S 批第 4 项）**：
  按 Claude Code 2.1.271 的 `.claude/output-styles` 契约落成本地输出样式发现与选择。新增
  `services/output_styles_defs.py`（两层发现、字段解析、审计投影、精确选择），改
  `services/agent/graph.py`（`SYSTEM_PROMPT` 拆成头 / 编程纪律 / 尾，新增
  `compose_system_prompt`，`initial_messages` 接受选中样式）、`services/agent/runtime.py`
  （每任务发现一次、`output_styles.loaded` 事件、选中样式经 `_RunScope` 带进系统提示）、
  `services/settings.py` + `schemas/settings.py` + `api/routes/settings.py`
  （`output_style` 设置键与顶层标量合并）、`api/routes/projects.py`
  （`GET /projects/{id}/output-styles`）、`web/src/components/settings/api.ts`（类型）。
  1. **发现与命名**：一个 md 一个样式，项目层 `<workspace>/.blank/output-styles/**/*.md`
     + 用户层 `~/.blank/output-styles`（`BLANK_USER_OUTPUT_STYLES_DIR`、
     `BLANK_USER_INSTRUCTIONS_DIR/output-styles` 可改）递归扫描（深度 32、目录数 4096
     上限），同名项目层胜用户层；扩展名匹配大小写不敏感、名字只删小写 `.md`
     （`Foo.MD` 保留后缀，与上游 `replace(/\.md$/,"")` 一致）。
  2. **字段**：`name` 默认文件名；`description` 默认正文首行（字符串 trim、布尔/数字转
     字符串、对象/数组告警后回退首行）；`keep-coding-instructions` 按上游 `m1` 口径
     （true/false 集合，`1` / `0` 也认）；`force-for-plugin` 只对 plugin 样式有意义，
     在用户/项目样式上解析后告警并忽略；未知键进 `ignored` 并告警、样式仍加载；坏
     frontmatter 降级（字段丢掉、正文保留）而不是跳过；超 1 MiB 跳过并点名；正文原样
     使用、不截断。
  3. **选择**：`settings.output_style`（默认 `"default"`）精确查表，名字区分大小写；
     空名/未知名字静默回落 `default`（上游 `?? null` 的同一语义），只在
     `output_styles.loaded` 的 `selected` / `active` 里留痕；归一化 trim，空串/超过 256
     字回落 `default`。
  4. **系统提示**：默认编程纪律段（Codex coding guidelines / Claude code editor 规则的
     本机压缩版）单独成段；选中样式且 `keep-coding-instructions !== true` 时该段被样式
     正文替换，`true` 时两段都在；样式段固定排在安全/审批/工作区边界之后并写明不得覆盖
     它们；没有选中样式时系统提示与旧版逐字节一致（sha256 基线
     `0c05c3558e3fc6da04ebe015cda0544c826c26c084bb7ca8536c6be854276111` 钉在判据里）。
  5. **可观察**：`output_styles.loaded`（发现结果 + selected/active，不含正文）与
     `GET /projects/{id}/output-styles` 列表（审计信息与字符数，不回正文）。
  测试：新增 `tests/test_output_styles_v47.py` 31 例（命名、字段、跳过与降级矩阵、两层
  覆盖、选择语义、合成逐字节基线、keep 两态、运行时系统提示与事件、设置键与 HTTP 列表），
  变异电池 21/21 红（逐条改坏实现，红在指定判据上；跑完按原字节还原并核对 sha256）。
  全量后端回归：**3293 passed, 28 skipped**（0 failed，rc=0，710.58s）。
  偏离：目录用 `.blank/output-styles` 而不是 `.claude/output-styles`，用户层是
  `~/.blank/output-styles`，刻意只读 `.blank`；没有 plugin 清单 / marketplace 两层，
  `force-for-plugin` 只解析并告警；Claude 内置 Explanatory / Learning / Concise 三个
  英文 persona 不移植；没有每轮注入的 `"<name>" output style is active.` 元提醒（本机
  每次任务/回合重建系统提示，样式在合成处已经生效）；设置键名是 `output_style` 而不是
  `outputStyle`。

- **自定义斜杠命令（`.blank/commands`）与参数展开对齐 Claude（S 批第 3 项）**：
  按 Claude Code 2.1.271 的 `.claude/commands` 命名规则与 `qW` 占位符引擎落成本地斜杠命令。
  新增 `services/prompt_args.py`（`qW` 等价替换引擎 + 路径占位符）与
  `services/commands_defs.py`（两层发现、命名规则、frontmatter 审计、用户输入展开）；
  改 `services/skills_defs.py`（`argument-hint` / `arguments` 解析与审计投影）、
  `services/tools/skills.py`（渲染委托 `render_prompt`、条目带参数名与 base_dir）、
  `services/agent/runtime.py`（`commands.loaded`、与 Skill 合并同一份目录）、
  `api/routes/tasks.py`、`api/routes/threads.py`（创建任务 / 会话回合 / 插话三个入口展开
  并落 `command.expanded`）、`api/routes/projects.py`（`GET /projects/{id}/commands`）、
  `services/agent/graph.py`（插话注入模型的是展开正文）。
  1. **命名与发现**：一个 md 一条命令，嵌套目录用 `:` 连接（`db/migrate.md` →
     `/db:migrate`）；子目录里的 `SKILL.md` 用目录名，根目录的 `SKILL.md` 没有名字、
     跳过；同目录多 md 且含 `SKILL.md` 时只用它并告警。项目层
     `<workspace>/.blank/commands` + 用户层 `~/.blank/commands`（`BLANK_USER_COMMANDS_DIR`、
     `BLANK_USER_INSTRUCTIONS_DIR/commands` 可改），同名项目层胜；单个坏文件不拖垮其余命令。
  2. **文件边界**：只认 `.md` 后缀，超 1 MiB 跳过并点名；坏 frontmatter 降级不跳过
     （与 Skill 同口径），正文超 20000 字截断并标注；`allowed-tools` / `disallowed-tools`
     只解析留审计、不改变工具池；`disable-model-invocation` 只挡模型清单与 `skill` 工具，
     `user-invocable: false` 只挡用户 `/名字` 入口。
  3. **参数替换（逐条对齐 `qW`）**：`$ARGUMENTS` 整串、`$ARGUMENTS[n]` / `$n` 取第 n 个
     shell 词（0 起，`shlex` 口径，引号不配对退回空白切分）、`$name` 按 frontmatter
     `arguments` 声明取位（最长名字优先、后接字母或 `[` 不算占位）、`\$` 转义、替换进去的
     值不会被二次展开；越界占位符保留原文且不算「用过」，因此没有别的占位符时仍追加
     `\nARGUMENTS: <args>`（与上游逐字节一致，`tests/test_skill_tool_v33.py` 里对应期望已同步）。
  4. **路径占位符**：正文里的 `${CLAUDE_PROJECT_DIR}` / `${CLAUDE_SESSION_ID}` /
     `${CLAUDE_SKILL_DIR}` 按上下文替换（后者只对 `SKILL.md` 命名的命令有值），Windows
     路径统一成正斜杠。
  5. **三个入口与模型侧**：用户输入 `/名字 参数` 在创建任务、会话回合、运行中插话三处展开——
     标题与界面保留用户原话，落库的 `goal` 与插话注入给模型的是展开正文；未知名字与非
     slash 文本原样返回。模型侧与 Skill 共用一份目录（同名命令胜），正文仍只经 `skill`
     工具按需加载。
  6. **可观察**：`commands.loaded`（发现结果）、`command.expanded`（含 name / source /
     kind / 字符数 / 打码路径，插话时带 `channel=steer`）与 `GET /projects/{id}/commands`
     列表（只给审计信息与一句话说明，不回正文）。
  测试：新增 `tests/test_commands_v46.py` 42 例（`qW` 各形态与越界语义、命名三形态、
  两层覆盖、跳过与降级矩阵、三个真实 HTTP 入口、运行时发现/清单/工具加载、插话读取侧），
  变异电池 41/41 红（逐条改坏实现，红在指定判据上；跑完按原字节还原并核对 sha256）。
  全量后端回归：
  **3262 passed, 28 skipped**（0 failed，rc=0，709.25s）。
  偏离：目录用 `.blank/commands` 而不是 `.claude/commands`，用户层是 `~/.blank/commands`，
  刻意只读 `.blank`；`!` 前缀的 shell 执行块不执行、原样留在正文里；未知 `/名字` 按普通
  文本入库（本机没有补全/选择器，把 `/path/to/file` 当错误会误伤正常消息）；团队群聊消息
  与定时/工作流等非手输任务不走用户侧展开；`allowed-tools` / `disallowed-tools` 只审计；
  `${CLAUDE_EFFORT}` 未移植；命令名按文件名原样、只差大小写的两条命令在 Windows 上不会同时存在。

- **文件型 Skill 定义（`.blank/skills`）与 skill 工具双闸对齐 Claude（S 批第 2 项）**：
  按 Claude Code 2.1.271 的 `.claude/skills` 契约落成本地磁盘 Skill 发现与合并。新增
  `services/skills_defs.py`（frontmatter 降级解析、字段校验、两层发现、审计投影、合并规则），
  改 `services/tools/skills.py`（目录带可用性/工具审计字段、模型清单与工具拒绝）、
  `services/agent/runtime.py`（每任务发现一次、`skills.loaded` 事件、与库目录合并、
  没有模型可调用项时摘掉 `skill` 工具）、`services/subagent.py`（子代理拷贝目录，
  没有一个模型可调用时同样摘掉入口）。
  1. **目录与发现**：一目录一 Skill（`<name>/SKILL.md`，文件名大小写不敏感）；项目层
     `<运行根>/.blank/skills` + 用户层 `~/.blank/skills`（`BLANK_USER_SKILLS_DIR`、
     `BLANK_USER_INSTRUCTIONS_DIR/skills` 可改），同名项目层胜用户层；调用名就是目录名，
     frontmatter `name` 只作显示名；没有 SKILL.md 的目录静默跳过，`synced` 保留目录、
     超 1 MiB、读失败逐个点名，单个坏文件不拖垮其余 Skill；刻意只读 `.blank`，
     不把仓库里的 `.claude/skills` 顺带加载。
  2. **坏 frontmatter 降级而不是跳过**：YAML 坏掉 / 未闭合 / 非映射时丢掉全部字段但
     仍加载文件——名字回落到目录名、说明回落到正文首行，问题进告警（与 Claude 同口径）。
  3. **说明与正文**：`description` 缺省时回落到正文首行，清单里截到 120 字；正文超过
     20000 字截断并标注，正文只在 `skill` 工具里按需加载，不提前进系统提示。
  4. **双闸与只解析字段**：`disable-model-invocation` 同时挡住模型清单与 `skill` 工具
     （工具拒绝时明确提示不要绕道复刻）；`user-invocable`、`allowed-tools` /
     `disallowed-tools`（含 `disallowedTools` 别名）只解析留审计，不改变工具池；
     `model` / `effort` / `hooks` / `mcpServers` / `lspServers` / `paths` / `context` /
     `agent` 等未支持字段逐条进 `ignored`。
  5. **合并与接通**：库 → 用户级 → 项目级，后者胜；但「模型不可调用」的同名项不能遮住
     可调用的那一份（对应 Claude 同名去重时保留可调用版本）。运行时每任务在最终运行根上
     发现一次并落 `skills.loaded`，系统提示只列模型可调用项；子代理拿目录拷贝，
     没有一个模型可调用时连 `skill` 工具一起拿掉。
  测试：新增 `tests/test_skills_disk_v45.py` 33 例（两层覆盖、跳过矩阵、坏 frontmatter
     三形态降级、字段解析与审计、合并遮蔽、真实运行时发现→提示→工具加载→事件、
     只有 user-only 时主代理与子代理都拿不到入口），变异电池 19/19 红（逐条改坏实现，
     红在指定判据上；跑完按原字节还原并核对 sha256）。全量后端回归：
     **3220 passed, 28 skipped**（0 failed，rc=0，748.88s）。
  偏离：目录用 `.blank/skills` 而不是 `.claude/skills`、用户层是 `~/.blank/skills`，
  且刻意只读 `.blank`；`allowed-tools` / `disallowed-tools` 只解析留审计、不改变工具池；
  正文超过 20000 字截断（Claude 原样使用）；未支持字段只进 `ignored`；plugin 命名空间、
  `synced` 同步根（只跳过、未实现同步）以及 `allowed-tools` 里的
  `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}` 替换未移植。S2 当时「本机还没有斜杠
  命令入口，`disable-model-invocation` 的 Skill 用户侧也没有触发路径」已由 S3 改写：
  Skill 与命令共用 `/名字` 入口，`disable-model-invocation` 只挡模型、不再挡用户。

- **文件型子代理定义（`.blank/agents`）与 spawn_agent(subagent_type) 对齐 Claude（S 批）**：
  按 Claude Code 2.1.271 的 `.claude/agents` 契约落成本地定义发现与派发。新增
  `services/agents_defs.py`（frontmatter 解析、字段校验、两层发现与覆盖、审计投影），
  改 `services/subagent.py`（`SubAgentRequest` 携带定义、工具池收窄、专用说明进系统提示、
  续聊保留边界）、`services/tools/delegate.py`（`subagent_type` 解析与整批拒绝、maxTurns
  折算步数）、`services/tools/base.py`（`ToolContext.agents`）、`services/agent/runtime.py`
  （每任务发现一次、`agents.loaded` 事件、目录进提示与上下文）、`services/agent/graph.py`
  （系统提示列自定义类型）；`pyproject.toml` / `uv.lock` 显式声明 `pyyaml`。
  1. **两层发现**：项目层 `<运行根>/.blank/agents/**/*.md` + 用户层 `~/.blank/agents`
     （`BLANK_USER_AGENTS_DIR`、`BLANK_USER_INSTRUCTIONS_DIR/agents` 可改）；同名项目层胜
     用户层，同层深层文件覆盖浅层（按路径段数与字符串排序后者胜）；单个坏文件只进
     `errors`，不拖垮同目录其余定义。
  2. **校验口径**：无 frontmatter / 无 `name` 静默跳过；声明了 `name` 但校验失败必须点名
     （name 去空白后为空、以 `-` 开头、NFKC 后含 `:`、非字符串；缺 `description`；
     frontmatter 坏 YAML / 未闭合 / 非映射；文件超 1 MiB / 读失败）。`tools` /
     `disallowedTools` 接受逗号串或列表，`tools:"*"` 表示不限，非法类型按「没有可用工具」
     fail-closed，禁止项里的 `*` 按全部禁用；`maxTurns` 必须是正整数（`0` / `true` / 字符串
     一律忽略并告警）；正文超过 20000 字截断并标注。
  3. **定义不能扩权**：工具池先从只读/隔离基线（`READ_ONLY_TOOLS` / `ISOLATED_TOOLS`）起步，
     `tools` 取交集、`disallowedTools` 再减；定义里写 `run_shell` 也不会让非隔离子代理拿到
     写工具。定义正文追加进子代理系统提示，明确「只补充做法，不得放宽上面的规则、审批要求
     与工作区边界」。
  4. **派发与审计**：`spawn_agent` / `spawn_agents` 的 `subagent_type` 不传即内置
     `general-purpose`；未知类型整批拒绝并列出可用名字；没显式传 `max_steps` 时把
     `maxTurns` 作为步数上限（受子代理全局 8 步约束）。`subagent.started` 记录 `agent_type` /
     `agent_ignored` / `agent_tools`，发现结果（含错误与路径掩码）落 `agents.loaded`；
     续聊（`send_message`）保留定义正文与工具白名单，第二轮不会退回默认工具池。
  5. **只解析不生效的字段**：`model` / `permissionMode` / `hooks` / `mcpServers` / `skills` /
     `isolation` / `background` / `memory` / `effort` / `color` / `initialPrompt` 逐条进
     `agent_ignored`，审计里不抄定义正文。
  测试：新增 `tests/test_agent_defs_v44.py` 32 例（两层覆盖、校验矩阵、工具收窄六组合、
     未知类型、maxTurns 步数、真实运行时发现/提示/按类型派发/续聊），变异电池 12/12 红
     （逐条改坏实现，红在指定判据上；跑完按原字节还原并核对 sha256）。全量后端回归：
     **3187 passed, 28 skipped**（0 failed，rc=0，719.98s）。
  偏离：目录用 `.blank/agents` 而不是 `.claude/agents`，用户层是 `~/.blank/agents` 而不是
  `~/.claude/agents`；Claude 的 plugin / flagSettings / policySettings 三层没有移植，只保留
  用户层与项目层；工具名沿用本地 snake_case，定义里写 `Read` / `Bash` 这类 Claude 别名不会
  命中本地工具（结果是拿不到工具，不是悄悄放宽）；`model` / `permissionMode` / `hooks` /
  `mcpServers` / `skills` / `isolation` / `background` / `memory` / `effort` / `color` /
  `initialPrompt` 只解析不生效；正文超过 20000 字截断（Claude 原样使用）；fork / remote 两种
  子代理形态与按 plugin 命名空间引用未移植（fork 已于 T 批补齐、remote 冻结，见最新条目）。

- **交互问答与两类提案：AskUserQuestion / ProposeGoal / ProposeSkills 对齐 Claude（R 批第 6 项）**：
  按 Claude Code 2.1.271 的 AskUserQuestion、ProposeGoal、ProposeSkills 契约落成本地
  `request_user_input`（重写）、`propose_goal`、`propose_skills` 三个工具。改
  `services/tools/ask.py`（归一化、参数校验、preview 片段判定、答案解析与渲染），新增
  `services/tools/goals.py`、`services/tools/skill_proposals.py`；`tools/skills.py`
  （目录带上单独写的 description）、`tools/__init__.py`、`permissions.py`（两个提案工具
  进只读集合：目标落在会话行、提案落在 skills 表，都不写工作区）、`agent/graph.py`
  （问题门、目标结算、工具规划与系统提示）、`agent/runtime.py`（每轮从库重读会话目标）、
  `services/threads.py`（session_goal 读/写/清）、`models/skill.py` + `models/thread.py`、
  `schemas/skill.py` + `schemas/task.py` + `schemas/thread.py`、`routes/skills.py`
  （提案采用）、`routes/tasks.py`（答题回执恢复）、`routes/threads.py`（session-goal
  端点）、迁移 `0035_session_goal_skill_meta.py`；前端 `bot/turn.tsx`、`bot/bot-app.tsx`、
  `tasks/*`、`skills/*`（提案分区与状态口径）、`workbench.tsx` 接线。
  1. **问答契约**：1-4 题、每题至多 4 个选项（下界不卡，事实题可以没有选项），header 截到
     12 字、question 截到 500 字、选项 label/description 截到 120 字；`multiSelect` 与
     `preview` 互斥，preview 只收片段（拒绝整页/脚本标签、注释、代码围栏，≤4000 字）；
     单选「默认/推荐」每题只留一个，多选保留全部；显式 `default` 必须命中已列出的 label。
  2. **答案回执**：`{answers: {问题文本→答案}, response?, annotations?}`，多选答案列表按
     ", " 合并，注解按问题文本或 header 归位；`notes` 以「备注：」、`response` 以「补充：」
     进模型，preview 不回贴——模型看到的是结论，不是自己的渲染物。
  3. **目标提案**：`propose_goal` 缺省要用户确认，确认走普通问题卡（id 合成 `goal1`、
     header「会话目标」、选项「设为本次会话目标 / 不用」、刻意没有默认值），接受后写回
     `threads.session_goal`，下一轮起每轮从库重读并以「以下是用户在本次会话中确认的目标」
     注入（不得覆盖安全与审批）；自由文本答案当作用户改写后的目标接受，拒绝或空答不落任何
     东西；`GET/DELETE /threads/{id}/session-goal` 可读可清，`goal.set` 事件留痕。
  4. **技能提案**：一次最多 3 条，`kind` 为 `new` 或 `improvement`；`new` 落成
     `status="proposed"` 的新行，`improvement` 必须 `target` 命中本任务已加载过正文的
     Skill（没加载过不让提改进），否则整批拒绝并点名第几条。`POST /skills/{id}/accept`
     采用：`new` → `draft`；`improvement` 把旧正文 append 进 `history`、`version+1`、
     换成提案正文并删掉提案行；三种 409 各有错误码（`skill_not_proposed` /
     `skill_proposal_invalid` / `skill_target_not_found`）。
  5. **界面**：问题卡多选可切换、自由文本可覆盖、选中项带 preview 时按纯文本 `<pre>`
     渲染（不进 innerHTML），每题都有推荐项才给「按默认继续」；技能页新增「待采用的提案」
     分区，可采用/丢弃（丢弃二次确认），状态未知值不兜底成 draft/active。
  测试：新增 `tests/test_ask_goal_skills_v43.py` 51 例（纯函数契约、工具落库与采用、
     真实 HTTP 多选/注解/自由文本往返、目标提案确认与拒绝、目标安装到会话并在下一轮注入），
     变异电池 14/14 红（逐条改坏实现，红在指定判据上；跑完按原字节还原并核对 sha256）。
     前端 `tsc --noEmit` rc=0，`node --test` 299 passed / 0 failed。全量后端回归：
     **3155 passed, 28 skipped**（0 failed，rc=0，838.92s）。
  偏离：工具名沿用本地 snake_case（`request_user_input` / `propose_goal` /
  `propose_skills`）；`afkTimeoutMs`（用户离开自动超时）未落地，问题卡没有倒计时；
  preview 前端只按纯文本渲染，不执行模型给的 HTML/Markdown；问题卡没有单独的注解/补充
  输入框，`annotations.notes` 与 `response` 目前只走 API 侧；目标确认卡 id 固定合成
  `goal1`，工具调用 id 不透传；`session_goal` 写入按 2000 字截断（提案正文上限 500 字）；
  提案落库到 `skills` 表而不是写 skill 文件，采用 `new` 后是 `draft`、要用户再启用；
  会话目标按 thread 记，不跨团队成员共享。

- **LSP 工具：单工具九操作、文本与计数对齐 Claude（R 批第 5 项）**：
  按 Claude Code 2.1.271 的 `sdk-tools.d.ts` 契约与 `claude.exe` 嵌入实现落成本地
  `lsp` 工具。新增 `services/lsp/client.py`（stdio 帧、按扩展名懒启动、反向请求应答、
  失败缓存、关进程）与 `services/tools/lsp.py`（参数校验、九操作映射、`git check-ignore`
  过滤、文本格式化与计数）；`tools/base.py`（`ToolContext.lsp`）、`services/agent/runtime.py`
  （每任务一个管理器、收尾关闭）、`tools/__init__.py`、`tools/registry.py`（按需、并行安全、
  别名）、`subagent.py`（子代理工具表不含 `lsp`）接线。
  1. **一个工具、九个操作**：`goToDefinition` / `findReferences` / `hover` /
     `documentSymbol` / `workspaceSymbol` / `goToImplementation` / `prepareCallHierarchy` /
     `incomingCalls` / `outgoingCalls`。`filePath` 收绝对路径或工作区相对路径；
     `line`/`character` 都是 1-based，只在发请求处减一。`incomingCalls`/`outgoingCalls`
     先 `textDocument/prepareCallHierarchy`、再用返回的第一条 `{item}` 问
     `callHierarchy/incomingCalls`（`outgoingCalls` 同理）。
  2. **文本格式逐字对齐**：单条定义 `Defined in path:l:c`、多条 `Found N definitions:`
     每行两空格；单条引用 `Found 1 reference:` 换行两空格，多引用
     `Found N references across M files:` 后按文件空行分组 `path:` + `  Line l:c`；
     工作区符号同分组并带 ` (Kind) - Line N in container`；hover `Hover info at l:c:`；
     documentSymbol `Document symbols:` 两空格递进 + ` - Line N`；调用层级
     `Found N incoming/outgoing call(s):` + `[calls at: l:c]` / `[called from: l:c]`；
     八条空结果文案照抄，模型据此判断要不要再查一次。
  3. **计数与过滤**：`resultCount`/`fileCount` 按 Claude 口径；定义/引用/实现/工作区符号
     四类结果先问 `git check-ignore`（每批 50、5s 超时，非仓库不过滤），被 `.gitignore`
     排除的生成文件不进结果。
  4. **失败说清楚**：文件不存在 / 不是文件 / 超过 10,000,000 字节（提示用 ceil MB）直接
     拒绝；没配服务端回 `No LSP server available for file type: .ext`；初始化失败回
     `LSP initialization failed: ...` 并记住失败，不在每次调用重拉；运行期失败（超时、
     畸形数据）回 `Error performing <op>: ...` 而不是抛异常——与 Claude 的 catch 同口径。
  5. **协议与生命周期**：`Content-Length: N\r\n\r\n<body>` 帧；`initialize` 后
     `initialized`；服务端反向 `workspace/configuration` 按项回 `null`、其余已知方法回
     `null`、未知方法回 `-32601`；每次请求前全文 `didOpen`/`didChange`（不装 watcher）；
     每任务一个管理器（绑定 `run_root`），任务收尾（含等审批）关进程，恢复后按需重拉；
     子代理工具表不含 `lsp`，语言服务器进程不会跟着隔离副本泄漏。
  测试：新增 `tests/test_lsp_tool_v42.py` 40 例（真实 stdio 假服务端跑通九操作、位置换算、
     文本格式、计数、gitignore 过滤、10MB 上限、启动崩溃、失败缓存、配置跳过、关闭幂等、
     运行时注入与收尾关闭），变异电池 34/34 红（逐条改坏实现，红在指定判据上，跑完按原字节
     还原并核对 sha256）。
  偏离：工具名 `lsp`（Claude 为 `LSP`）；服务器清单读本机 `lsp.json`（跟设置文件同目录，
  `BLANK_LSP_FILE` 可改）；没有文件 watcher，每次请求重发全文 `didChange`；初始化失败
  按扩展名缓存、不每次重拉；子代理不含该工具；`lsp.json` 写坏一条只跳过该条；非 runtime
  入口走模块级兜底管理器。全量后端回归：**3104 passed, 28 skipped**（0 failed，rc=0，
  713.15s）。

- **MCP 资源四工具：能力先于调用、失败按服务端隔离、刷新只动本地工具表（R 批第 4 项）**：
  按 Claude Code 2.1.271 的 `sdk-tools.d.ts` 与 `claude.exe` 嵌入实现落成本地
  `list_mcp_resources` / `read_mcp_resource` / `read_mcp_resource_dir` / `refresh_mcp_tools`。
  新增 `services/tools/mcp_resources.py`；`services/mcp/client.py`（资源能力判定、翻页、
  非标准目录方法、`MCPError.rpc_code`），`services/mcp/protocol.py`（`qualify_prefix`），
  `tools/gateway.py`（`mcp_tool_names` / `add_tools` / `remove_tools` / `sync_mcp_tools`），
  `tools/registry.py`（`add` / `remove` 与四工具别名、按需登记、并行安全集合），
  `tools/base.py`（`ToolContext.mcp`）、`agent/runtime.py`（注入 `MCPBundle`）接线。
  1. **能力先于调用**：没在 `initialize` 里声明 `capabilities.resources` 的服务端连
     `resources/list` 都不发；没声明目录扩展的服务端连非标准方法都不发——「没这个能力」与
     「能力坏了」必须能分开说。`resources/list` 跟 `nextCursor` 翻页；`resources/directory/read`
     首页 `InvalidParams` ＝ 不是目录，后续页坏掉保留已拿到的前几页，页数上限 20 并回报
     `truncated`。
  2. **失败按服务端隔离**：一家失败只进 `data.errors`，另一家照常返回；全部声明了能力的
     服务端都失败时明确报错，不说 `No resources found`——那句会把「没人给我资源」和
     「每家都炸了」说成同一件事。「全部失败」按「报错数 == 有能力服务端数」判定，一家失败、
     另一家只是没资源不算。
  3. **读取口径**：文本原样返回；二进制落系统临时目录（`blank-mcp-resources/<task_id>/`，
     不进用户工作区）并给路径，正文只留一行说明；非法 base64 不抛异常、如实标「未保存」；
     `-32601` / `-32002` 按 Claude 原文分支处理，后者只在服务端声明了目录能力时才补
     `ReadMcpResourceDir` 提示。
  4. **刷新只动本地工具表**：只重查已连接客户端、绝不重拨；连过但失败的服务端报
     `not_connected` 而不是「不存在」；每服务端回 `{server, status, toolCount, added, removed}`。
     同名工具就地换定义并保留 `tool_search` 拉出来的激活状态，只摘本服务端前缀下的工具，
     场景禁用的名字在刷新后仍不可用。
  测试：新增 `tests/test_mcp_resources_v41.py` 34 例（客户端协议/工具语义/刷新/注册与装配，
  含真实 stdio 子进程、分页、目录后续页损坏、翻页上限、二进制落盘、场景禁用回放、运行时
  注入）；连同相邻 17 个测试文件 **528 passed, 1 skipped**；变异电池 37/37 全红（逐条改坏
  实现、实测红在预期用例上，跑完按原字节还原并核对 sha256；首轮两条存活变异已回填判据：
  假服务端资源改成分页、注册表定义也要换）。
  全量后端回归：**3063 passed, 28 skipped**（0 failed，rc=0，878.10s）。
  偏离：目录能力门控键名（Claude 的编译期常量）无法从产物恢复，按语义放宽为
  `capabilities.extensions` 里任一 `directoryRead === true`；二进制落系统临时目录而不是
  Claude 的内部存储，但「给路径、不展开正文」的契约一致；列表/读取结果以 JSON
  （`{"resources"}` / `{"contents"}`）输出而不是 Claude 的文本模板；全部失败时回明确错误
  而不是 `No resources found`；目录截断只以 `data.truncated` 表达、不在文本里追加说明；
  工具名沿用本地 snake_case（`list_mcp_resources` 等）而不是 Claude 的
  `ListMcpResourcesTool`；刷新是否保留激活状态在拆解产物里读不出来，这里显式钉成「保留」。

- **任务清单 Task*：依赖图 + 可阻断钩子（R 批第 3 项）**：
  按 Claude Code 2.1.271 的 `sdk-tools.d.ts` 契约与 `claude.exe` 里的嵌入实现落成本地
  `task_create` / `task_get` / `task_update` / `task_list`。新增 `services/tools/tasks.py`；
  `tools/__init__.py`、`tools/base.py`（`ToolContext.hooks`）、`tools/registry.py`（别名）、
  `services/hooks.py`（两个阻断事件）、`agent/runtime.py`（钩子提到 ToolContext）、
  `agent/graph.py`（编排纪律）接线。
  1. **落盘语义**：一个任务一个 `<id>.json`，id 是十进制数字、单调递增，删除后不复用
     （高水位单独记；建文件与写水位之间崩溃丢了水位文件，删除最高号任务时会把水位抬回去）。
     列表按数字 id 升序，不是文件名字典序；坏文件跳过而不是整表失败；写盘走 scratch +
     `os.replace`，同目录一把 asyncio 锁 + `open(..., "x")` 独占创建，并发创建各自拿到不同 id。
     任务 id 只认 `[A-Za-z0-9_-]{1,64}`，穿越目标真实存在也读不到。
  2. **依赖图**：`addBlocks` / `addBlockedBy` 写双向引用（A.blocks → B、B.blockedBy → A），
     已存在的过滤掉，过滤后非空才记入 `updatedFields`；目标不存在时忽略但字段仍如实上报；
     删除任务把其它任务里对它的引用一并清掉。
  3. **展示口径**：`updatedFields` 只列真正变了的字段（同状态不算变更、不产生
     `statusChange`）；`metadata` 按键合并、置 `null` 删键；`task_list` 隐藏
     `metadata._internal` 为真的任务、把已完成阻塞者从 `blockedBy` 里过滤，渲染
     `#id [status] subject (owner) [blocked by #a, #b]`，空表 `No tasks found`；
     团队任务标 `in_progress` 未带 owner 时自动署名，按 group_id 共享同一份清单。
  4. **可阻断钩子**：`task_created` 在创建落盘后跑，被拒就把刚建的任务删回去；
     `task_completed` 在状态落盘前跑，被拒时任何字段都不落盘。两个事件进
     `BLOCKING_EVENTS`，退出码 2 生效，async 标志在阻断事件上被忽略。
  测试：新增 `tests/test_task_tools_v40.py` 33 例（落盘语义/依赖图/展示口径/参数与钩子/工具面，
  含并发创建、坏文件、崩溃窗口、真实子进程退出码 2）；相邻 14 个测试文件 **271 passed**；
  变异电池 16/16 全红（逐条改坏实现、实测红在预期用例上，跑完按原字节还原并核对 sha256）。
  全量后端回归：**3025 passed, 28 skipped**（0 failed，rc=0，737.97s）。
  偏离：四个工具常驻而不是 Claude 的 `shouldDefer`（纯展示差异）；更新不存在的任务回
  `Task #<id> not found` 而不是 error；空 `subject` / `description` 明确拒绝，Claude 的 zod
  允许空串；团队清单按 group_id 共享，但没有移植 `task_assignment` 消息；钩子事件名用
  snake_case `task_created` / `task_completed`；不存时间戳、不发 UI 通知。

- **REPL 工具：一段持久的 JavaScript 把多次工具调用串起来（R 批第 2 项）**：
  按 Claude 原文（`code` 必填、支持顶层 await、变量跨调用保留；`description` 可选；
  `timeout` 默认 30000、上限 600000）落成本地 `repl`。新增 `services/agent/repl.py`
  （每任务一个持久 Node 子进程 + 密封 `vm` 上下文 + JSON 行协议）与
  `services/tools/repl.py`（工具面/回执/审计）；`tools/__init__.py`、`tools/base.py`、
  `tools/gateway.py`、`tools/registry.py`、`agent/runtime.py`、`agent/scenarios.py`、
  `agent/placeholders.py`、`subagent.py` 接线。
  1. **会话语义**：同一任务的多次调用共用进程与上下文，顶层 `let/const/class/function`
     跨调用保留；顶层 await 自动用 async 包装；最后一条顶层表达式即返回值（先按顶层 `;`
     切分，无分号时按行自底向上累积并用真编译验证续行）；`console.log/error` 分回
     stdout/stderr。
  2. **密封与边界**：`vm` 上下文没有 `require` / `process` / `import`；动态 `import()` 会
     报错但不杀会话；内层工具按本地名字注入，每次调用回宿主走同一条
     `ToolGateway.execute(..., inner=True)`（场景禁用/安全策略/权限/审计/沙箱一个不少）；
     会话/编排类工具与场景排除的工具不注入；需要审批的内层调用直接 `denied`（REPL 里弹不出
     审批卡，放行等于绕过权限）。
  3. **超时与崩溃**：`timeout` 默认 30000ms、上限 600000ms；同步死循环由 `vm` 自身超时打断、
     会话保留；异步挂起由宿主截止时间杀掉进程并清空状态；未处理的异步异常只记到当前调用，
     不会把空闲会话打死。
  4. **环境与收尾**：缺 Node 报 `skipped` + `data.missing_command=node` 并接入停滞检测；
     没有网关的入口（子代理、非 runtime）直接拒绝；子代理工具表移除 `repl`；任务收尾摘会话
     （等审批那一档保留）。
  测试：新增 `tests/test_repl_tool_v39.py` 36 例（契约/参数边界/注入过滤/内层拒绝与透传/
     回执渲染/审计事件/宿主超时与崩溃回执/真实 Node 持久+TLA+密封+同步超时保留+异步超时重建/
     场景·占位·注册表·子代理·收尾接线）；连同相邻 7 个测试文件合计 **220 passed**；子代理与
     边界 5 个文件 **37 passed**；变异电池 16/16 全红（逐条改坏实现、实测红集合，跑完即按原
     字节还原）。2026-11-12 后端全量回归 **2992 passed / 28 skipped / 0 failed**
     （720.25s，rc=0；R1 基线 2956 + 本批 36 例，跳过数与基线同为 28，全是本机环境门：
     docker、受限令牌自检、aiomysql、桌面包产物）。
  偏离：内层工具用本地名字（`read_file`/`run_shell`/`search_code` 等）而不是 Read/Bash 别名；
  Claude 的顶层声明持久是语言级作用域，本机用「扫描顶层声明 + `Object.assign(globalThis, ...)`」
  近似回填，同名遮蔽与解构声明不在近似范围内；REPL 里没有 web/Agent/Workflow 等会开进程或
  等审批的入口；需要审批的动作只能改成顶层工具调用；超时或进程崩溃会清空整个会话状态；
  `vm` 不是内核级沙箱，脚本的能力边界靠「不注入 `require`/`process`/`import` + 内层调用走网关」，
  真正的高风险动作仍由网关权限与沙箱档位把关。

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
  路径仍要求**工作区相对路径**（Claude 用绝对路径）。链式编辑已核对上游原文：Edit 只要求
  「本会话内读过、`old_string` 精确匹配且唯一」（`E:/tmp/rea7/claude_tools_dump.txt` 第
  230–231 行），没有「`old_string` 是上一次 `new_string` 子串就拒绝」这条规则；我们写成功后
  重记文件状态，读一次即可连续编辑，判据 `test_three_chained_edits_after_one_read`。
  抓包原文不入库，只留在 `E:/tmp/rea6`、`E:/tmp/rea7`。

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

- **插件技能目录根即技能加载（嵌套 Skill 目录，Nkn 批）**：上游插件技能加载器 `Nkn`
  的语义是——先检查插件技能目录自身有没有 `SKILL.md`，有则整个目录就是一个 Skill，
  立即返回（不扫描子目录），名字取 `${pluginName}:${frontmatter name}`（frontmatter
  缺省时取目录名）；没有 `SKILL.md` 才 `readdir` 扫描子目录。用户层加载器 `Fkn`
  只扫描子目录，不检查根 `SKILL.md`。此前 kbgai 的 `discover_skills` 只调
  `_skill_dirs(plugin_dir)` 扫描子目录，从不检查插件技能目录自身的 `SKILL.md`。
  本批在插件技能目录扫描前加根即技能检查：先调 `_definition_from_dir(plugin_dir)`
  判目录自身是否含 `SKILL.md`，非空则以 `plugin:` 命名空间加载并 `continue` 跳过
  子目录扫描；名字用 `display_name or name`（frontmatter name 优先，缺省取目录名），
  与上游 `Nkn` 同口径。用户层与项目层不改动，仍只扫描子目录（与上游 `Fkn` 一致）。
  判据 `services/api/tests/test_skill_nested_v53.py`（7 例：根即技能加载、frontmatter
  名字优先、缺省取目录名、根技能时跳过子目录、无根技能正常扫描子目录、用户/项目层
  不检查根 SKILL.md、多 skills 目录混合）。变异 4/4 红（撤掉根即技能检查、撤掉
  `continue`、名字只取目录名不用 frontmatter、给用户/项目层加根检查），每条红在
  指定判据上。相邻 6 套件 158 passed。全量回归见提交记录。

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
