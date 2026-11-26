# 验收留存记录 · 2026-11-21

## 范围

本次记录覆盖 2026-11-21 的 agent 工作回路升级：发布门禁、AGENTS.md 检查表、界面验收 journey、dev 前置预检、验收脚本相对路径修复、新增 Skill/Command/Agent 资产、kbgai 产品内核加固（安全存储、错误边界、429 错误契约、异常日志），以及整体页面视觉统一升级（设计 token、外壳、导航、卡片与顶栏层级）和右侧任务面板的 Qoder 式层级重排。

## 执行命令与结果

### 执行式界面验收 journey

```bash
PYTHONUTF8=1 "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
```

- 退出码：`0`
- 打开断言：3 条（卡片数 3、列数 2、无三列空占位）
- 动作与持久化断言：12 条动作/断言记录，包含添加、拖拽、刷新、过滤
- 结论行：`结论：通过：打开有东西，动作有变化，刷新还在`

### 前端类型检查

```bash
E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit
```

- 退出码：`0`

### 前端单测

```bash
E:/env/node/node-v24.20.0-win-x64/node.exe --test --test-concurrency=2 --import ./test-bootstrap.mjs "src/**/*.test.ts"
```

- 结果：`pass 435`、`fail 0`
- 退出码：`0`

### 页面视觉统一升级

- 全局 token 改为四层表面（bg / canvas / sidebar / surface）与独立 hover/selected 状态；品牌色统一为克制蓝，深色皮肤同步派生。
- `AppShell`、首页主区与任务工作台统一为 48px 顶栏、20px 水平留白、14px 圆角主卡和同一档标题层级。
- `PageBody` / `Panel` 统一内容宽度、标题分隔线与卡片阴影；`AppRail` 使用 hover/selected token，避免灰阶互相顶替。

### 右侧结果区按 WorkBuddy / Qoder 重做

- `TaskPanel` 从 320px 任务侧栏改为默认 520px 的结果区：顶部改成 WorkBuddy 式浏览器标签栏，支持打开/切换/关闭“改动、文件、产物、预览”和单个文件标签；文件标签显示文件名，点击后读取工作区文件内容。
- 标签栏下新增文件标题行：文件名、路径、当前文件数/状态、黑色“分享”按钮；分享复制当前文件路径或结果标题，并短暂显示“已复制”。
- 面板左边缘支持拖拽调宽（360–860px，且不超过视口减去主区 320px），双击恢复默认，键盘焦点下左右方向键微调、`Home` 复位；宽度写入 `blank:bot-panel-width`。全屏按钮会把结果区铺满窗口，退出后恢复原宽度。
- “改动”视图按 Qoder Review 的三段式组织：工具栏（本会话/本轮范围、文件搜索、目录分组切换）、文件列表、常驻 diff；次级视图保留变更、证据、活动，证据与活动改为结果区内的卡片分区和轨迹视图。
- “文件”视图调用 `fetchTree` 浏览当前项目工作区，点击文件会打开为新标签页；“产物”视图汇总新写入文件并支持打开文件标签；“预览”视图提供新标签页打开本地页面的入口，避免结果区嵌套自身。
- 文件标签固定最小宽度并禁止被长文件名挤扁，标签栏改为横向滚动，避免“改动/文件”被压成单字。
- 图片文件通过新增的 `GET /projects/{id}/file/raw` 以 `image/*` 返回，前端用 blob URL 在结果区直接预览；文本文件仍走原有 `/file` JSON 接口。
- `CodeChangeView` 支持平铺/目录分组文件列表，复用 `parseDiff` / `diffStats` / `langOf` 与现有语法高亮，显示新增/删除行数、变更类型与逐行 diff。
- 改动数据从单一当前轮次扩展为“当前轮次 + 本会话所有轮次”的证据聚合：打开会话后按 turn 拉取 evidence，按路径合并并优先保留带 diff 的记录；没有会话证据时仍回退到本轮步骤里的文件变更。
- 这不是工作区实时 `git diff`，而是 Agent 执行过程留下的文件改动审计；后端目前只有 task 级 evidence/diff，没有 thread/项目级聚合端点。
- `web/next.config.ts` 将本机局域网地址 `192.168.2.111` 加入 `allowedDevOrigins`，避免通过该地址打开时 Next dev 拦截 HMR/前端资源。
- `tsc --noEmit` 为 `0`；`eslint src/components/bot/panel.tsx src/components/projects/api.ts` 为 `0`；435 例前端测试全过；`tests/test_projects_v02.py` 10 例通过，覆盖新增 raw 图片路由；dev server 编译无 stderr。
- 用 Chrome headless 在 1440×900 下截图复核：右侧面板已呈现标签栏、文件标题行、分享按钮与 Qoder 三段式改动区；“改动 / 文件”标签完整显示，不再被挤成单字。

### 后端错误契约测试

```bash
.venv/Scripts/python.exe -m pytest tests/test_error_contract_v153.py tests/test_rate_limit_config_v152.py -q
```

- 结果：`5 passed`
- 退出码：`0`
- 覆盖：429 嵌套错误契约、旧扁平错误体兼容、限流配置

### Workflow YAML 解析

- `ci.yml` 与 `publish.yml` 均通过 Python `yaml.safe_load` 解析。

### 资产基线

- Qoder：Skills=1、Commands=1、Custom Agents=1、Hooks=2、Rules=1、Workflows=2
- Claude：Skills=1、Commands=1、Custom Agents=1、Hooks=1、Rules=2、Workflows=2
- Claude inventory：skills=1、subagents=1、rules=3、commands=1、hooks=1
- lint findings=0、errors=0、warnings=0

### Git 证据

- `git rev-parse --is-inside-work-tree`：`true`
- 历史画像：453 次提交（近 30 天 337 次），confidence=high
- 回归证据：检查 200 次提交，保留 20 个源码/测试同变候选
- 新增资产已 `git add` 暂存，未提交

## 复核边界

- 全量 API 测试曾在 10 分钟超时，未完成，不作为通过证据；本次记录采用相关测试 5 例通过的结果。
- `eslint src` 仍报 6 个既有 `react-hooks/set-state-in-effect` 错误与 4 个 warning；其中 `app-rail.tsx` 两处是既有 effect 写法，本次只改样式 token，`error.tsx` 的 `<a>` 导航错误已修。
- 无 opencode 会话适配，`eligibleCount=0`。
- 真实 PR / 发布决定与恢复行为未被会话或宿主证据观测。
- 本记录只证明当前窗口修复状态与执行证据，不代表 later comparable outcome。

## 2026-11-21 会话级审批授权（主线 A / T-A1）

### 范围

- 同一会话（thread）内，用户当面批准「本会话允许这类动作」后，同类动作不再逐条挂卡；
  `bypass` 档本身不产生任何会话级授权。
- 类别按命令位置精确识别；删除、推送、数据库迁移按是否不可逆拆类，复合命令要求每段同类或无害。
- 被明确驳回的精确调用先于不可逆闸门生效，不再反复弹卡。
- 前端审批卡新增「本会话允许这类动作」档位与对应文案。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_session_approval_grants.py tests/test_permission_modes_v11.py \
  tests/test_approval_card_v101.py tests/test_v165_phase4_modes.py \
  tests/test_approval_persist_v43.py tests/test_settings_permissions_v50.py \
  tests/test_validation_approval_identity_v101.py tests/test_authorize_single_entry_v417.py \
  tests/test_approval_v06.py tests/test_permission_rules_v112.py \
  tests/test_permission_profile_v48.py -q
```

- 结果：`216 passed`，退出码 `0`（最后一次源码改动后复跑）

```bash
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test \
  --import ./test-bootstrap.mjs "src/**/*.test.ts"
```

- 结果：`pass 437`、`fail 0`，退出码 `0`

```bash
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit
```

- 退出码 `0`

```bash
PYTHONUTF8=1 "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" \
  scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
```

- 退出码：`0`
- 打开断言：3 条（卡片数 3、列数 2、无三列空占位）
- 动作与持久化断言：12 条动作/断言记录，包含添加、拖拽、刷新、过滤
- 结论行：`结论：通过：打开有东西，动作有变化，刷新还在`

### 复核边界

- 未跑全量 API 测试（约 20 分钟）；本次只跑权限/审批相关的 216 例。
- 会话授权的撤销 UI 尚未接入；后端存储已提供 `revoke` / `purge_thread`，删除会话时会清理。
- eslint 既有 6 个 `react-hooks/set-state-in-effect` 错误未处理，与本次改动无关。

## 2026-11-21 任务工作台状态真相（主线 B / B1）

### 范围

- 任务已成功、计划却一步都没回写时，计划面板、任务列表与对话页计划卡显示「计划未更新」
  并说明“以时间线的实际执行为准”，不再摆一个会被读成“什么都没做”的 `0/4`。
- 证据库列表徽标：没有验收条件时显示「没有验收条件」，不再写 `0/0 验收`。
- 失败/取消任务的 `0/4` 保持原样——那是有效信息，不能也改成一句说明。

### 执行命令与结果

```bash
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test \
  --import ./test-bootstrap.mjs "src/**/*.test.ts"
```

- 结果：`pass 439`、`fail 0`，退出码 `0`（最后一次文案/兼容改动后复跑；新增
  `planIsStale` / `planProgressText` / `criteriaChipText` 三组判据）

```bash
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit
```

- 退出码 `0`

```bash
PYTHONUTF8=1 "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" \
  scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
```

- 退出码：`0`；结论行：`结论：通过：打开有东西，动作有变化，刷新还在`

### 复核边界

- 未做真机浏览器截图；改动是状态文案逻辑，单测覆盖三个纯函数，界面渲染需人工验收。
- 未跑后端测试；本次未改后端。

## 2026-11-21 交付未完成不再判成功（主线 C / C1）

### 范围

- 交付文本自述「未达…门槛」「需要你：补足…」时，Verifier 的 `delivery_says_done`
  判 `false`，任务经 `resolve_status` 返回 `failed`，不再把未完成判成 `succeeded`。
- 覆盖数值门槛类表述（未达 / 未达到 / 未达标 / 未满足 / 还差 / 少于 / 字数不足等）
  与补做类交接（补足 / 补全 / 补齐 / 补写 / 补上等）。
- 保留「需要你：手动删除验证残留」这类收尾交接不判失败，避免误伤正常交付。

### 取证

- 历史任务 `a69c01d2`（2026-11-20）的 `write_file` 参数截断已有恢复路径；
  `tests/test_tool_args_truncation_recovery_v478.py` 与 `test_truncated_tool_call_v175.py`
  共 8 例通过。
- 2026-11-21 受控验证任务 `e08fc1db`：要求 `large-check.md` 不少于 9000 字符，
  实际 6291 字符，交付自述「未达 9000 字门槛」，任务却判 `succeeded`；
  本修复后同类交付判 `failed`。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_delivery_incomplete_v489.py tests/test_empty_response_zero_delivery.py \
  tests/test_done_checks_v427.py tests/test_delivery_unverified_v479.py \
  tests/test_verifier_skipped_validation_v409.py tests/test_verify_hook_v427.py \
  tests/test_verify_timeout_v427.py tests/test_path_verification_v168.py \
  tests/test_unverified_not_failed_v427.py tests/test_autorepair_verdict_v112.py \
  tests/test_tool_args_truncation_recovery_v478.py tests/test_truncated_tool_call_v175.py -q
```

- 结果：`88 passed`，退出码 `0`（新增 3 例在 `test_delivery_incomplete_v489.py`）

### 复核边界

- 本次只修「状态真相」，不提升模型完成大文件的能力：受控任务因 `max_steps=8`
  提前收尾，仍需 C2 处理「可量化验收 + 自动修复」。
- 未跑全量后端测试（约 20 分钟）。

## 2026-11-21 可量化验收 + 自动补齐（主线 C / C2）

### 范围

- `done_when[].check` 新增 `file_min_chars`（按 Unicode 字符数，不按字节）。
- `set_goal` 工具可声明该判据；执行节点写回 `goal_spec` 时保留 `done_checks`。
- 验证节点在机器判据没过时把 `validation.status` 改成 `failed`，路由回
  `execute_action`，并注入 `repair.started` 指令：要求用 `run_shell` 实测
  （如 `wc -m`），补足后重新核对；修复预算用尽后交审查/收尾，最终按失败状态收尾。
- Verifier 仍只在终态读事实，不因“模型写了判据”而改判。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_goal_checks_v490.py tests/test_validation_gate.py \
  tests/test_done_checks_v427.py tests/test_plan_workflow_v397.py \
  tests/test_transition_explain_v432.py tests/test_readonly_and_budget.py \
  tests/test_autorepair_verdict_v112.py tests/test_delivery_incomplete_v489.py -q
```

- 结果：`103 passed`，退出码 `0`（其中 `test_goal_checks_v490.py` 8 例）

### 端到端事实

- API 层验证：`POST /projects/{id}/tasks` 的 `goal_spec` 能持久化
  `done_checks=[{"type": "file_min_chars", "path": "large-check.md", "min": 9000}]`。
- 受控任务 `596a4dda`：自然语言要求 9000 字符，模型实际写入 **9498 字符**，
  任务 succeeded，过程中用 `run_shell` 实测过字数。
- 说明：该受控任务的 `goal_spec` 在测试请求构造时为空（PowerShell JSON 形状问题），
  所以没有触发 `goal.check_failed` 分支；该分支由 `test_goal_checks_v490.py`
  的 8 条判据覆盖，API 持久化由上面的独立验证确认。

### 复核边界

- 未跑全量后端测试；未做“真实模型先写短、再由修复回路补齐”的端到端，
  该路径由单测覆盖，建议在人工验收中用真实模型再跑一次。

## 2026-11-21 验收结论重试与未判定（主线 C / C3）

### 范围

- 执行型任务的验收结论要求 JSON；第一次解析失败、不是 JSON 对象或缺
  `acceptance` 字段时，用更硬的格式要求重试一次。
- 两次都失败时，验收结果记 `covered=null`（未判定），不再记 `covered=false`
  （没覆盖）；交付证据文本明确写「未判定」。
- 前端证据面板的验收标题增加「N 条未判定」，不再用 `0/4` 暗示四项都失败。

### 取证

- 修复前 evals T01–T03 基线：3/3 succeeded，`acceptance_coverage=0.636`；
  T02 四项因“模型未返回可解析的验收结论”全部被标成未覆盖。
- 修复后重跑 T02：succeeded，`acceptance_coverage=1.0`。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_acceptance_retry_v491.py tests/test_delivery_snapshots_v153.py \
  tests/test_delivery_unverified_v479.py tests/test_done_checks_v427.py \
  tests/test_empty_response_zero_delivery.py tests/test_goal_checks_v490.py \
  tests/test_delivery_incomplete_v489.py -q
```

- 结果：`52 passed`，退出码 `0`

```bash
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test \
  --import ./test-bootstrap.mjs "src/**/*.test.ts"
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit
```

- 前端单测：`pass 439`、`fail 0`；`tsc` 退出码 `0`

```bash
E:/env/node/node-v24.20.0-win-x64/node.exe evals/run_evals.mjs \
  --api http://127.0.0.1:8000/api/v1 --provider <deepseek-id> \
  --only T02 --concurrency 1 --approve-high
```

- 结果：`T02 succeeded`，`acceptance_coverage=1.0`

### 复核边界

- 评测的 `acceptance_coverage` 仍只数 `covered=true`；未判定不会被算成覆盖。
- 未跑全量后端测试；评测产生的临时项目与工作区已清理。

## 2026-11-21 审批过期治理（主线 C / C4）

### 范围

- 审批卡默认有效期从 30 分钟放宽到 **24 小时**；`BLANK_APPROVAL_TTL_SECONDS`
  仍可调短，夹在 60 秒 ~ 24 小时之间。
- worker 的过期清理跟随同一函数；超过 24 小时仍收敛为 failed，界面给重试入口。

### 取证

- 历史 26 条失败任务里 14 条是「审批已过期」；最典型的是人离开工位后卡片过期，
  任务直接结束、工作作废。
- 默认 30 分钟对“先确认一下再回来”不够；24 小时让审批卡在一天内保持可批。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_v163_super_agent.py tests/test_validation_approval_identity_v101.py \
  tests/test_approval_v06.py -q
```

- 结果：`45 passed`，退出码 `0`

### 复核边界

- 没有做续期接口或临期通知；24 小时后仍会失败，可重试。
- 未跑全量后端测试。

## 2026-11-21 上下文账本与旧工具参数折叠（主线 C / C5）

### 范围

- `_chars()` / `message_tokens()` 把 `tool_calls[].function.arguments` 计入上下文
  水位；此前只数 `content`，write_file 正文和 apply_patch 补丁不在账上。
- `_microcompact` 对最近 8 条之外的写类工具调用参数做有界折叠：正文/补丁超过
  1200 字符时换成一句“已折叠，需要时用 read_file 查看”。不删消息，配对不变。
- `MicroStats.args_shrunk` 进 `context.measured`，可解释省了什么。

### 取证

- 复跑 shopbench 时 `context.breakdown` 显示 assistant 角色 91k 字符，几乎全是
  write_file 的正文；旧水位却只看 content，压缩会低估。
- 折叠只动发给模型的旧消息副本，不改事件/证据/磁盘；最近窗口原样保留。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_context_layers_v12.py tests/test_context.py tests/test_context_compactor.py \
  tests/test_context_pairing_v14.py tests/test_context_watermark_v13.py \
  tests/test_compaction_window_v23.py -q
```

- 结果：`48 passed`

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_context_overflow_recovery_v403.py tests/test_idle_compaction_v427.py \
  tests/test_context_restart_v427.py tests/test_compact_continue_note_v427.py \
  tests/test_token_trimming.py tests/test_context_layers_v12.py -q
```

- 结果：`44 passed`

### 复核边界

- 没有做“当前文件状态快照”的大改造；本次只折叠旧调用参数。
- 未跑全量后端测试；`args_shrunk` 的真实任务读数待下一轮长任务确认。

## 2026-11-21 真实大任务复跑与自我修复（主线 C / C6）

### 范围

- 用原 shopbench 目标（LR10D 商品资料 PDF）在独立临时工作区复跑当前版本。
- 复跑任务 `7ce98397`：27 步 succeeded，11 个文件，`npm test` 18 项通过；
  原 `a69c01d2` 的 10706 字符 `write_file` 截断没有重演。
- 复跑交付自述「前端导航点击无反应，五个视图进不去」——C1 未完成词表因此补上
  「无反应 / 进不去 / 打不开 / 不工作 / 重跑」等表述。
- 修复任务 `37032c7b`：Agent 自己改 `public/app.js` 的 `switchView`，32 步 succeeded。
- 独立复核用 `services/api/scripts/browser_probe.mjs`：
  - `file://`：16/16 导航断言通过；14 条控制台错误均为 `file://` 下 fetch `/api/*`
    的 CORS（页面需要自己的服务端）；
  - `http://127.0.0.1:4173`：16/16 断言通过，**0 控制台错误、0 失败请求**，
    商品数据正常加载。

### 执行命令与结果

```bash
# 复跑与修复任务由任务 API 创建，状态从 /tasks/{id} 读取
# 独立浏览器复核：
E:/env/node/node-v24.20.0-win-x64/node.exe services/api/scripts/browser_probe.mjs \
  --url http://127.0.0.1:4173 --steps data/_shopbench_steps.json ...
```

- 结果：修复任务 succeeded；HTTP 探针 `"ok":true`、`error_count:0`、`assert_failed:0`。
- 合并回归：C1–C6 相关后端 16 个测试文件 **128 passed**；前端 **439 passed**；
  `tsc --noEmit` 退出码 0。
- 最终界面 journey：退出码 0，结论行 `结论：通过：打开有东西，动作有变化，刷新还在`。

### 复核边界

- 复跑任务的历史状态仍是 succeeded；按新词表，其交付（“无反应/进不去”）应判 failed。
  不改写历史，后续同类按新规则。
- 生成的 app 需要服务端；正确启动方式 `cd data/_shopbench_rerun && $env:PORT=4173; node server.js`。
- 修复任务交付仍写「需要你：…跑一次…确认」；是否阻塞留待下一轮。

## 2026-11-21 生成应用去 AI 味与安全加固（D1）

### 范围

- 生成应用 `data/_shopbench_rerun` 的视觉重做：纸面/石墨底色 + 单一琥珀强调色，
  平面面板、细线分隔、等宽读数，AI 视图改为「任务 / 结果 / 日志」三栏，去掉渐变、
  卡片阴影、KPI 卡片墙与图标装饰；监控视图补上读数单位。
- 生成应用安全与健壮性：
  - 静态页与接口统一 CSP、`nosniff`、`DENY`、`no-referrer`、`same-origin`、
    `Permissions-Policy`；
  - 静态路径解码失败/空字节返回 400，越出 `public/` 返回 403；
  - 请求体上限 256 KiB；
  - AI Base URL 只允许 http(s)、拒绝账号密码、长度上限；配置写盘 `chmod 600`；
  - `patch`、`code`、`level`、`task` 做类型与取值校验，非法输入返回 400。
- 把两类要求写回生成侧：`CODING_INSTRUCTIONS` 新增「前端不要做通用 AI 仪表盘」
  与「后端/脚本安全底线」两条，后续生成任务默认带上。

### 执行命令与结果

```bash
cd data/_shopbench_rerun && node --test
# 22 passed / 0 failed

# 独立浏览器探针（app 以 node server.js 启动在 4173）：
E:/env/node/node-v24.20.0-win-x64/node.exe services/api/scripts/browser_probe.mjs \
  --url http://127.0.0.1:4173 --steps data/_ui_verify_steps.json ...
# 9/9 断言通过；0 控制台错误；0 失败请求

cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_output_styles_v47.py tests/test_coding_discipline_v26.py \
  tests/test_prompt_parts_v427.py tests/test_two_scenes_v401.py -q
# 99 passed
```

- 探针断言覆盖：监控视图激活、单位 `m`、读数不含 `undefined`、AI 三栏存在、
  总览视图可返回。
- `SYSTEM_PROMPT` 钉住的哈希随提示扩充同步更新为
  `657164d93cf6e3b211f22ed2adfda19c0d5f377288240fc1be67bbf0d883f140`。

### 复核边界

- 本轮只覆盖**生成应用 + 生成侧提示约束**；主仓库全量代码规范与安全审计未做，
  需要单独立卡。
- 生成应用未纳入主仓库，仍是 `data/_shopbench_rerun` 下的展示与复跑产物。
- `chmod 600` 在 Windows 上可能无效，密钥首选环境变量。
- CSP 为 `'self'`，后续若引入外部字体/CDN 会被拦截，需要同步调整策略。

## 2026-11-21 harness 生成质量闸门（D2）

### 范围

- **前端交付闸门**：本轮改了前端文件（`.html/.css/.jsx/.tsx/.vue/.svelte`，或任务
  文本像前端时的 `.js/.mjs/.cjs`），验证节点要求最后一次前端写入之后有
  `browser_check` 回执，并且：控制台 0 错误、0 个失败请求、至少 1 条通过的断言、
  至少 1 条通过的动作；不满足就判失败并回执行节点修复。
- 浏览器不可用（没有 node/Chrome/探针）记 `unavailable`，不误判成页面坏了。
- 审查清单新增：反 AI 仪表盘、前端真验、安全底线（路径穿越/请求体上限/输入校验/
  http(s)/密钥/CSP/回环绑定）、代码规范、结论与工具事实一致。
- 修复本轮真机暴露的并行工具图片消息顺序 bug：带图 user 消息不再插在 tool 消息之间。
- 修复 `browser_check` 的 `file://` 越界：必须落在工作区内，远程 host 拒绝。
- 交付文本在无自动化测试但浏览器自检通过时，写明「已做浏览器自检」，不再只剩
  「未跑校验」。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_ui_delivery_gate_v492.py tests/test_browser_check_v427.py \
  tests/test_image_passthrough_v427.py tests/test_delivery_unverified_v479.py \
  tests/test_goal_checks_v490.py tests/test_transition_explain_v432.py \
  tests/test_v165_phase2_wiring.py tests/test_product_gaps_ui_v166.py \
  tests/test_doing_tasks_prompt_v167.py tests/test_autorepair_verdict_v112.py \
  tests/test_validation_approval_identity_v101.py tests/test_validation_gate.py \
  tests/test_readonly_and_budget.py tests/test_delivery_incomplete_v489.py \
  tests/test_acceptance_retry_v491.py tests/test_output_styles_v47.py \
  tests/test_coding_discipline_v26.py tests/test_prompt_parts_v427.py \
  tests/test_two_scenes_v401.py -q
# 272 passed
```

- 真机任务 `5a814453`：succeeded / 7 步，browser_check 带断言与动作，控制台 0 错误。
- 真机任务 `c0ceba72`：succeeded / 7 步，交付文本出现
  「已做浏览器自检（browser_check 通过：…1 个断言 / 6 个动作）」，事件
  `ui.check_passed` 落盘。
- 真机任务 `f31d0504`：修复前三个并行 browser_check 触发
  `insufficient tool messages following tool_calls` 400；已复现、修复，并在后续
  任务中确认不再出现。

### 复核边界

- 本轮没有加反 AI 样式的静态扫描器；视觉质量仍靠提示 + 审查清单 + 浏览器证据。
- 闸门要求浏览器回执，因此依赖本机 node/Chrome；环境缺失时按 `unavailable` 记录，
  不伪造通过，也不把环境问题算成页面缺陷。
- 主仓库全量规范/安全审计仍未做，需要单独立卡。
- 临时验证工作区与任务（`data/_ui_gate_probe*`、`5a814453`、`c0ceba72`）保留供查看。

## 2026-11-21 主仓库代码规范与安全审计（D3）

### 范围

- 前端：ESLint 6 error / 4 warning 清零，`lint`/`check.sh` 改为 `--max-warnings=0`；
  `tsc` 0；React 19 effect 写法修正；
  Markdown 链接协议白名单（阻断 `javascript:`/`data:`/`file:`/`//host`）。
- API：统一安全头（nosniff/DENY/no-referrer/same-origin/frame-ancestors）；
  上传附件与工作区 `/file/raw` 的 html/svg/xml/js/css 降级为 `text/plain`；
  provider `base_url` 只允许 http(s)、拒绝 URL 内凭据；token 定长比较；
  422 不回显 Pydantic `input`。
- 桌面壳：外链只放行 http(s)；主窗口导航守卫；`blank:open-path` 只允许目录；
  `app://` 静态资源路径按分隔符对齐、非法编码/空字节 404；协议响应补安全头。
- 权限：移除 `host_bypass` 的会话级授权（越界执行只能逐条批），修正 T-A1 交接单。
- 工具说明预算：压缩 `set_goal` schema，常驻工具总量回到 22500 以内。
- bash 解析：跳过 WindowsApps 的 WSL 启动器，优先 Git Bash/MSYS2。
- HTTP 钩子：回环地址不走系统代理（修掉本机 `127.0.0.1:1` 被代理回 503 的假红）。
- `check.sh` 增加前端 ESLint 步骤。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q
# 首轮：4990 passed / 5 failed / 28 skipped，17:55
# 5 条失败：host_bypass 会话档、工具预算、bash WSL 挂起、fs_broker 测试用错文件、
#           hook 回环被代理。全部用产品侧修复（fs_broker 是测试自己用错 .png）。
# 第二轮：5000 passed / 1 failed / 28 skipped，17:40；该轮启动于 trust_env 修复前，
#         唯一失败是 hook 测试，单跑已 18 passed。
# 第三轮（最终）：5001 passed / 0 failed / 28 skipped，17:13，退出码 0。
```

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_security_headers_v493.py tests/test_projects_v02.py \
  tests/test_uploads_v15.py tests/test_auth_v02.py tests/test_auth_surface_v34.py \
  tests/test_permission_modes_v11.py tests/test_session_approval_grants.py \
  tests/test_sandbox_bypass_v115.py tests/test_tool_budget_v427.py \
  tests/test_shell_field_v313.py tests/test_fs_broker_v93.py \
  tests/test_error_contract_v153.py -q
# 176 passed
```

```bash
cd web && node node_modules/eslint/bin/eslint.js src   # 0 problems
cd web && node node_modules/typescript/bin/tsc --noEmit  # exit 0
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs "src/**/*.test.ts"  # 440 passed
cd web && node scripts/build.mjs                        # exit 0
cd . && node --test desktop/tests/*.test.cjs             # 104 passed
cd . && node --test evals/tests/judgement.test.mjs evals/tests/fetch-json.test.mjs  # 25 passed
```

- 后端最终全量：**5001 passed / 0 failed / 28 skipped**，17:13，退出码 0；
  60 条 warning 均为既有 `datetime.utcnow()` 弃用告警。

### 复核边界

- 未安装 ruff/mypy，也未做依赖漏洞扫描（按合同不新增依赖/不联网）。
- 未迁移 `datetime.utcnow()` 等弃用告警；涉及 naive/aware 语义，单独立卡。
- 未给 Next 页面加严格 CSP（需要 nonce/构建改造）；先加 nosniff/DENY/referrer。
- `host_bypass` 不再有会话档，这是有意的安全收紧，会多一次审批卡。
- 桌面 `app://` 的严格 CSP 仍未加；当前只补了 nosniff/DENY/referrer。

## 2026-11-21 依赖漏洞、严格 CSP 与弃用迁移（D4）

### 范围

- 前端依赖：`pnpm audit --prod` 发现 7 条 moderate，全部在 `next@16.4.1`；
  升级 `next` / `eslint-config-next` 到 **16.4.6**，审计归零。
- 后端依赖：`pip-audit` 发现 13 条（certifi / opentelemetry-api / pypdf）；
  升级到 `certifi 2026.11.20`、`opentelemetry-api 1.45.2`、`pypdf 6.21.4`，
  审计归零。
- 严格 CSP：构建后按每个页面的内联脚本算 SHA-256，注入
  `<meta data-blank-csp>`；`script-src 'self' 'sha256-…'` **不含
  `'unsafe-inline'`**。覆盖 `out/`（桌面静态导出）与
  `.next/server/app|pages`（`next start` 的预渲染 HTML）。
- 主题引导保留内联（首帧防闪白），由构建期哈希放行；未放宽脚本策略。
- `style-src` 保留 `'unsafe-inline'`（React style 属性需要）。
- 3 处 `datetime.utcnow()` 迁移为
  `datetime.now(timezone.utc).replace(tzinfo=None)`，保持 naive UTC 存储语义。

### 执行命令与结果

```bash
pnpm audit --prod --registry https://registry.npmjs.org   # 0 vulnerabilities
uv tool run pip-audit -r logs/api-requirements-audit.txt   # 0 vulnerabilities
cd web && node scripts/build.mjs                           # 服务端构建，CSP 15/15 + 2/2
cd web && BLANK_DESKTOP_BUILD=1 node scripts/build.mjs      # 静态导出，CSP 15/15
cd web && node --test ... "src/**/*.test.ts" scripts/csp.test.mjs  # 443 passed
cd web && node node_modules/eslint/bin/eslint.js src --max-warnings=0  # 0 problems
cd web && node node_modules/typescript/bin/tsc --noEmit     # exit 0
# next start :3010 浏览器探针：ok:true，0 控制台错误、0 失败请求
# 静态导出 :4180 探针：页面正常，无 CSP 违规（仅无 /api 代理的 404）
cd services/api && pytest 上传/附件/团队/会话相关测试        # 66 passed
```

- 后端全量：**5001 passed / 0 failed / 28 skipped**，17:15，退出码 0；
  warning 从 60 降到 20（`datetime.utcnow()` 已清零）。

### 复核边界

- 动态 SSR 页面不在构建期哈希 CSP 覆盖范围；当前应用全部静态预渲染。
- `next dev` 开发模式不带 CSP；生产构建才注入。
- 未做 SRI；`style-src` 未收紧到哈希。
- `pnpm audit` 需要官方 registry；npmmirror 不提供 audit 端点，未改项目配置。
- Next 16.4.1 → 16.4.6 与 pypdf 6.21.1 → 6.21.4 是 patch 级升级，仍需人工点验。

## 2026-11-22 新对话首屏（B2，极简两场景版）

### 范围

- 产品明确只保留两个场景：编程（主）/ 日常（辅）；文档 / 头脑风暴 / 逆向不开放
  新建，只留给老任务跑完。
- 首屏按用户最终口径收敛成两样：**大图标 + 大标题**，居中排；参考 Codex
  去掉副标题；中间的场景卡、随场景起手式、首次授权三档全部移除。主区标题栏
  保留「新对话」。
- 输入框位置不动：工作目录与场景仍在输入框下方工具条的原位置；撤销上一版的
  Codex 式上下文条，占位文案恢复按场景取 `inputHint`。
- **新对话默认没有目录**：标题为「我们要做什么？」，目录下拉显示「选择目录」；
  **无目录 = 纯聊天**：直接发送可用，后端禁用文件/命令工具，不拍快照、不进
  计划/校验；要求操作电脑时提示先选目录，不伪造执行，也不会沿用项目上次的
  活动目录。
- **选了目录 = 可操作电脑**：恢复完整工具与审批；后端的 `chat_only` 跟着任务
  落库，重试/恢复不会被项目后来绑的目录改写。
- 标题跟随目录：有目录时「我们要在 <目录> 做什么？」——不加「」或【】，目录名
  两侧留空格；切换目录后刷新项目数据并同步标题。
- 后端场景标签「开发」改为「编程」；编程提示词对照 Claude Code 的编程纪律，
  日常提示词对照 Codex 的通用 agent 口径。
- 目录选择改成真正可用的功能：桌面壳点「选择目录…」走系统目录框；浏览器没有
  原生目录框，改为打开后端目录浏览器（盘符 → 逐级进入 → 上一级 → 选择当前目录），
  不再要求手填路径。选中后自动绑定到当前项目并激活，只换目录不换会话。
  项目页的绑定表单同样改为只读路径 +「选择目录…」，绑定后自动切到新目录。

### 执行命令与结果

```bash
cd web && pnpm exec tsc --noEmit                                   # exit 0
cd web && pnpm run lint                                            # 0 problems
cd web && pnpm test                                                # 443 passed
cd web && pnpm run build                                           # exit 0；CSP 17/32（app 15/15 + pages 2/2）
cd services/api && .venv/Scripts/python -m pytest \
  tests/test_chat_only_v495.py tests/test_threads.py \
  tests/test_v165_phase2_units.py tests/test_scenarios_v153.py \
  tests/test_two_scenes_v401.py tests/test_auth_surface_v34.py \
  tests/test_run_lease_v102.py -q                                  # 88 passed
# 浏览器探针：
# 1) 无目录纯聊天：新对话不选目录发送「你好」→ succeeded、有回答、无报错。
# 2) 无目录要求改 README → 回答必须先选目录、提示点「选择目录」，不伪造执行。
# 3) 选目录后「纯聊天」提示消失，恢复完整 Agent 模式。
# 4) 目录浏览器 29 步：逐级进入 / 上一级 / 选另一个目录 / 切回原目录。
# 5) 标题与旧对话 19 + 8 步：标题「我们要在 _ui_gate_probe2 做什么？」（无「」），
#    打开旧对话仍显示目录，再按「新对话」清空。
# 全部 0 控制台错误。
```

### 复核边界

- 首屏不再展示场景/授权/起手式；它们只在输入框工具条里，默认授权档是「询问」。
- 文档 / 头脑风暴 / 逆向仍留在 `SCENARIOS` 供老任务完成，但不在 `/scenarios`
  清单里；改回开放需要先补权限与提示词验收。
- 未做 WorkBuddy 的仓库/分支启动器与连接应用引导。
- 切换目录依赖 `refreshProjects()` 回读；网络失败时标题会停留在旧目录。
- thread/task 没有记录 workspace：打开旧对话只能跟随项目当前活动目录，无法
  精确还原它当初实际使用的目录；要精确还原需要数据库变更。
- 目录浏览器依赖后端枚举主机目录，接口要求 token 且默认只监听回环地址；
  后端若暴露到局域网，持有 token 的人可以枚举目录名。
- 探针新增的临时 workspace 绑定已通过 API 解绑，活动目录恢复原值。

- **纯聊天边界当时的已知缺口**：选目录后说「只回复 OK，不要读取或修改任何
  文件」仍会翻代码。该缺口已在 B4 用 `no_tools` / `read_only` 硬边界修复；
  见下一节。
- 探针期间创建的纯聊天/模式切换测试会话已取消并永久清理。
- 清理时发现 `DELETE /api/v1/threads/{id}/permanent` 导入/清理错表
  （`AgentEvent` / `AgentCheckpoint` / `SearchDoc`），已修正并补测试；
  `test_threads.py` 23 passed，真机永久删除 5 条测试会话成功。

## 2026-11-22 场景策略与否定式指令（B4）

### 范围

- **否定式指令成为硬边界**：
  - `no_tools`（只回复 / 不要读取 / 不要调用工具）：这一轮工具表为空，直接
    作答；任务级标记落库，重试/恢复不会重新挂工具。
  - `read_only`（不要修改 / 不要执行）：先于模型判定锁 readonly，按**只读执行**
    跑——不是“给判断”。模型判 act/advise 都会归一到只读执行，执行节点只放行
    读工具，步数收到 8，交付走正常总结。
- **清单输出格式**：用户明确要清单时，系统规则要求 Markdown 列表一项一行、
  文件/路径用反引号，不能把多项用「、」串成一段；`ANSWER_INSTRUCTION`
  同步放开 5 行限制。
- **只要清单的任务不再读文件内容**：新增 `list_only`；工具表只留
  `list_files / glob`，拿到第一份目录回执后直接进收尾；收尾用工具回执拼
  Markdown 层级列表，不再让模型改写或总结用途。
- **场景策略层**：`ScenarioPolicy(planning, verification, budget_scale)`。
  - 编程：代码规划 + 代码验证 + 100% 预算。
  - 日常：简洁规划 + 通用验证 + 80% 预算；系统提示明确“简单任务不必凑步骤”，
    但代码/页面的验证下限不降。
  - `task.started.scenario_policy` 记录本轮策略，证据页可追溯。
- 未做：按场景切模型、场景级评测集、日常应用集成；复杂语义否定仍可能认不出。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python -m pytest \
  tests/test_directives_v496.py -q                                # 11 passed
cd services/api && .venv/Scripts/python -m pytest \
  tests/test_intent_and_reply.py tests/test_v165_phase2_units.py \
  tests/test_agent_v04.py tests/test_two_scenes_v401.py \
  tests/test_threads.py tests/test_chat_only_v495.py \
  tests/test_directives_v496.py tests/test_dispatch_v162.py \
  tests/test_scenarios_v153.py tests/test_transition_exhaustive_v485.py \
  tests/test_auth_surface_v34.py -q                                # 176 passed
cd web && pnpm exec tsc --noEmit && pnpm run lint && pnpm test     # 443 passed
./check.sh --no-smoke --no-acceptance                              # 全部通过
# 真机探针：
# 1) 选目录后「只回复 OK，不要读取或修改任何文件」→ 约 9 秒回 OK、0 工具动作；
# 2) 「不要修改任何文件，只列出这个目录里有哪些文件」→ task.started.readonly=true、
#    list_only=true，工具仅 list_files / glob；第一次目录回执后
#    execute_action → summarize_result，结果由工具回执直接拼成层级 Markdown 列表，
#    不再读文件内容、不再让模型写总结；
# 3) 「列出三种常见排序算法，每种一行」→ 三条 Markdown 列表，界面纵向项目符号；
# 4) task.started.scenario_policy 按场景记录，日常预算 80%（单测）。
```

### 复核边界

- 规则式否定只收明确说法；复杂语义否定认不出时按原链路走，仍可能过度执行。
- `read_only` 任务仍可能花 20–30 秒做只读勘察；这是 B5 要计入的成本/速度问题。
- 场景策略首版只有规划提示、验证提示、预算比例真实生效；硬安全下限两场景
  共用，不能借场景放宽。
- 本轮探针创建的测试会话已永久清理；用户原有的 `ok` 会话保留。
- `read_only` 任务的工具表仍会列出写工具（执行层硬拒），下一步可在装配层
  收窄成只读可见表。
- `list_only` 的层级列表来自第一次 `list_files` 回执；模型若传的 depth 很浅，
  更深层文件不会出现。下一步可固定默认 depth 或用 `glob **/*` 补全。
- 首次真机复现时 `execute_action → summarize_result` 的边映射缺失，已修复并
  补路由测试；修复前产生的失败轮次不会自动重跑，需要新开一轮。

## 2026-11-22 四任务对标（B5）

### 范围

- 同一组 4 个任务对空白格 / Claude Code / Codex：T01 编程新增功能、
  T03 编程缺陷修复、T07 数据 CSV 日报、T25 文档 README 示例。
- 每个任务有 agent 看不到的隐藏验收；评测工作区在仓库外；Claude 用
  `--safe-mode`，Codex 显式 `workspace-write`；`.blank/` 不计入改动。
- 本次同时修掉对标暴露的两个产品缺陷：
  - `不要改变对外 API 名称` 被裸“改”正则误判成只读，导致 T03 0 改动；
  - 无目录时“做一个官网”被 `chat_only` 放行自动执行，而不是 `wait_user`。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_v165_supervisor.py tests/test_directives_v496.py \
  tests/test_intent_and_reply.py tests/test_scenarios_v153.py \
  tests/test_chat_only_v495.py tests/test_transition_exhaustive_v485.py \
  tests/test_transition_explain_v432.py tests/test_dispatch_v162.py \
  tests/test_dispatch_runtime_v162.py -q                          # 148 passed
cd services/api && .venv/Scripts/python.exe -m pytest -q
# 5024 passed / 1 failed / 28 skipped
# 唯一失败：B4 改系统提示后未同步的 SYSTEM_PROMPT 哈希；
# 更新哈希后 test_output_styles_v47.py 单跑 31 passed（全量未重跑）
node evals/compare.mjs --agents ours,claude,codex \
  --only T01,T03,T07,T25 --timeout 300 \
  --provider 158e133a-d9d6-486c-9e9a-add5c2cadb14 \
  --api http://127.0.0.1:8065/api/v1 --cleanup
# ours 96.5 / claude 97.5；双方 4/4；ours 225K vs claude 128K token/任务
# Codex: unavailable（默认免费模型 404；gpt-5.6-luna 402 额度不足）
```

- 最终结果：`evals/results/compare-2026-11-22T10-37-35-830Z.json`
- T03 修复后单跑：`evals/results/compare-2026-11-22T10-32-37-054Z.json`（100 分）
- 报告：`docs/capability-benchmark-2026-11-22.md`

### 复核边界

- 只有 4 个小任务；隐藏验收由本仓库编写，不能据此宣称整体超过 Claude。
- 我们与 Claude 是同一模型家族但不是严格同模型：`deepseek-flash` vs
  `deepseek-flash[1m]`。
- Claude 的自证只能从最终文本判断，T25 的 90 分是下限，不是“没跑测试”的定论。
- Codex 模型/额度不可用，未得到有效成绩；对外必须写“不可用”，不能写 0 分。
- 下一步优先查 T25 的 16 轮/399K token 浪费，再扩到 8–12 个任务。

## 2026-11-22 T25 成本收敛（B6）

### 范围

- 只改 T25 这类“只改文档、不改源码”的任务成本；不扩任务集、不改 UI。
- 修复链式命令解释器替换：`set "PYTHONPATH=src" && python -c ...` 的第二段
  原来不替换，Windows 返回 9009，模型再 `where`/`py` 重试，白烧两轮。
- 文档-only 任务收窄工具表（34→16）并追加执行纪律：只读目标文档/直接引用
  源码、不扫全仓、不写临时校验脚本、除非明确要求不查 git/浏览器/任务台账、
  最后跑一次 `run_tests`。
- 用户显式 `set "PATH=..."` 时保留其解释器选择；Docker 档不回退宿主
  `sys.executable`。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_directives_v496.py tests/test_tool_budget_v427.py \
  tests/test_legacy_fixes_v162.py tests/test_shell_relative_exec_v161.py \
  tests/test_run_tests_detect_v152.py tests/test_claude_tool_surface_v28.py \
  tests/test_task_tools_v40.py tests/test_v165_supervisor.py \
  tests/test_chat_only_v495.py -q                                  # 161 passed
# T25 真机（DeepSeek，隐藏验收）
# 文档纪律版：156K / 10 轮 / 23s / 100 分
# 稳定性复跑：136K / 9 轮 / 16s / 100 分
# 对照：Claude Code 138K / 14 轮 / 29s
# T01 普通编程回归：155K / 8 轮 / 13s / 100 分
# 四任务最终复跑：ours 98.7 / Claude 94.4，双方 4/4
# Claude 自证未检测到，94.4 是下限；补回约 99.4，整体仍接近
# ours 166K/19s，Claude 154K/26s；T25 ours 135K vs Claude 178K
cd services/api && .venv/Scripts/python.exe -m pytest -q
# 5033 passed / 28 skipped，18:44，退出码 0
```

- 结果文件：`evals/results/compare-2026-11-22T11-28-36-284Z.json`、
  `compare-2026-11-22T11-31-01-050Z.json`、
  `compare-2026-11-22T11-42-38-208Z.json`（四任务最终）
- 报告：`docs/t25-cost-2026-11-22.md`

### 复核边界

- 完整后端 **5033 passed / 28 skipped**、四任务已跑；未做人工界面验收。
- 文档-only 只在“README/文档 + 不改源码/只改文档/验收未修改 src”时生效；
  没写边界仍走全量工具表。
- 文档任务若实际需要浏览器/git，会被排除；这类任务不应标成“只改文档”。
- 两次 T25 有模型波动（156K/136K），不能把 136K 当作固定值。
- Codex 在 B6 运行时仍是旧 0.154+nex（404/402）；之后更新到 v0.171.0+
  spark-x2.6，**交互式 TUI 正常**（node_repl 读文件/校验），但 `codex exec`
  自动化 T01 在 workspace-write/danger-full-access 下都 300s 超时、apply_patch
  写出语法错误；这是自动化入口/终端链路问题，尚不能算有效编码对比。

## 2026-11-22 真实多文件任务复跑（B7）

### 范围

- 用原 shopbench LR10D 商品资料任务（PDF 附件），在仓库外干净工作区
  `_bench_workspaces/b7-shopbench-ours` 从零搭建 Web 运营工作台。
- 记录状态、步数、审批/介入、token、文件、测试、浏览器证据与失败点。
- 第一轮暴露并修复了产品缺陷：`不要修改工作区之外的文件` 被 `_NO_WRITE`
  误判成只读，导致 0 改动却 succeeded；`directives.py` 增加 `_OUT_OF_SCOPE`，
  补回归后 **98 passed**，第二轮才真正开始搭建。

### 执行命令与结果

```bash
# 第一轮（修复前）：0 改动 / 2 步 / 103,978 token / succeeded
# 任务 ID：0f0e423f-1afe-47a1-8367-3c61e6ef6c1e（第二轮）
# 任务状态：failed
# 步数 26 / 200；29 次模型调用；token 1,741,293
# 文件 18 次变更事件 / 12 个不同文件
cd _bench_workspaces/b7-shopbench-ours && node --test test/api.test.js
# 9 passed / 0 failed
cd _bench_workspaces/b7-shopbench-ours && PORT=4174 node server.js
node services/api/scripts/browser_probe.mjs \
  --url http://127.0.0.1:4174/ --steps data/b7-steps.json \
  --shot data/b7-shot.png --shot-open data/b7-open.png --wait 1000
# 13/13 动作/断言通过；唯一控制台错误 /favicon.ico 404
```

- 关键失败链：`ui.check_failed` → `repair.started` → `budget_exhausted`
  → `task.failed`。
- 报告：`docs/b7-shopbench-2026-11-22.md`
- 交接单：`ops/HANDOFFS/B7-claude.md`

### 复核边界

- 页面基本可用：商品、指标、模式、故障注入、规则引擎巡检均通过独立浏览器
  动作；失败在“没跑 browser_check + 1.5M 预算在修复前耗尽”，不是“不会写”。
- 只有一次运行、一个模型、一次 bypass 自动化；没有正常审批、没有 Claude/
  Codex 同题对照。
- 1.74M token 的真实任务成本、favicon 404、UI 修复截断留给 B8。
- 生成 app 保留在 `_bench_workspaces/b7-shopbench-ours`，未纳入主仓库。
- **B10 后续复跑**：任务 `c328c027-...` **succeeded**，28 步 / 32 调用 /
  1,757,693 token / 16 变更 / `npm test` 17 passed / browser_check 2 次 /
  验收 3/3；B7 的最终结论以此为准，前文保留失败分析。

## 2026-11-22 前端交付闸门自修（B8）

### 范围

- 预算耗尽时保留一次 UI 修复；该回合允许 browser_check/run_shell；
- browser_check 的 expect/steps 提示：初始元素 vs 点击后元素；
- 生成页面自带 favicon；
- 用 B7 复跑 + 同一工作区的 UI 自检小任务验证。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_ui_delivery_gate_v492.py \
  tests/test_transition_exhaustive_v485.py \
  tests/test_transition_explain_v432.py \
  tests/test_output_styles_v47.py \
  tests/test_directives_v496.py tests/test_tool_budget_v427.py -q    # 116 passed
# B7 全量复跑（最终提示词前）：failed，29 步 / 34 调用 / 1.69M token
#   ui_repair_reserve 生效，模型调用 6 次 browser_check，但初始断言写错
# 同一工作区 UI 自检小任务：succeeded，19 步 / 21 调用 / 844K token
#   9 expect + 6 动作全过，控制台 0 错误
#   注意：该任务预算未耗尽、任务书已明确检查顺序，不验证保留修复路径
# 最终提示词下从头复跑完整 B7：failed
#   28 步 / 33 调用 / 1,669,607 token；reserved_ui_repair=true 但
#   browser_check 调用 0 次；第二轮 ui_check 仍失败
```

- 报告：`docs/b8-ui-gate-2026-11-22.md`
- 交接单：`ops/HANDOFFS/B8-claude.md`
- evidence：`data/b8-evidence.json`、`data/b8-ui-repair-evidence.json`、
  `data/b8-final-evidence.json`

### 复核边界

- 小任务成功不等于完整 B7 一次跑完；最终提示词下从头复跑已失败：保留回合
  `reserved_ui_repair=true`，但模型一次 browser_check 都没调用。
- 只靠提示不稳定；下一步需要确定性强制/验证节点自动探测，不能继续加提示。
- UI 自检任务 844K token，偏贵；B8 只解决“差一步”，不解决大任务成本。
- 保留修复只有一次；第二次 UI 失败仍按预算收尾。
- favicon 是提示词底线，不是硬编码生成；仍需 browser_check 实测。

## 2026-11-23 确定性 UI 冒烟（B9）

### 范围

- 模型完全没跑 browser_check 时，验证节点自动启动工作区 `node <file>` 服务，
  用 browser_probe 做打开 + reload + 正文非空/无 undefined + 控制台零错误。
- 只做兜底，不替代模型交互验收；模型有 browser_check 时仍以模型结果为准。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_ui_delivery_gate_v492.py tests/test_tool_budget_v427.py \
  tests/test_transition_exhaustive_v485.py tests/test_output_styles_v47.py
# 60 passed
# 直接对 B7 工作区运行 auto_ui_check：
# {"status":"passed","url":"http://127.0.0.1:54974/","auto":true,
#  "asserts":1,"steps":2,"console":0}
```

- 完整 B7 从头复跑：**failed**，25 步 / 32 调用 / 1,709,213 token。
- 这次模型调用了 1 次 browser_check，但 click/type 后没有 wait；
  `#suggest-list .suggest`、`#chat-log .msg.ai` 在异步响应前断言失败；
  控制台 0 错误，接口测试 27 passed。
- 自动冒烟按设计只在“完全没有 browser_check 回执”时触发，因此这次没覆盖。

### 复核边界

- 自动冒烟只证明页面能打开、正文非空、控制台无错；不证明交互正确。
- 只支持 `package.json` 的 `node <file>` start；npm/shell/非 Node 返回 unavailable。
- 模型跑了 browser_check 但断言/时序写错，仍会阻塞交付；这是 B10 的问题。
- 没有跑完整后端 5000+；B9 相关 60 条通过。

## 2026-11-23 browser_check 动作可靠化（B10，人工验收）

### 范围

- 验收对象：B10 对 `browser_probe.mjs` 的三处修复（click/type 后 settle、
  长页面点击前 scrollIntoView、`assert {selector,min}` 读 selector）与
  `browser_check` 的 `settle_ms` 参数。
- 验收环境：Windows，本机 Node 24.20.0、Chromium（探针自动发现）、无 bash
  （`bash check.sh` 在本机报 `Bash/CallMsi/Install/REGD...`），按 `AGENTS.md`
  逐条执行 `check.sh` 等价命令；实际执行时间 2026-11-23 10:30–10:36
  （PowerShell 下以 `$env:PYTHONUTF8='1'` 运行，命令块保留 SKILL 的 bash 形态）。

### 执行命令与结果

```bash
# 1. 权威执行式界面验收（acceptance-journey SKILL 指定入口）
PYTHONUTF8=1 "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" \
  scripts/acceptance.py --journey acceptance/kanban.json \
  --page acceptance/fixtures/kanban.html
# 退出码 0；非 eval 断言 13 条全过（首屏 3/3、动作 10/10；
# 原始输出另含 2 条 eval 基准步，共 15 条记录）
# 结论：通过：打开有东西，动作有变化，刷新还在

# 2. B10 相关后端回归
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_browser_check_v427.py tests/test_tool_budget_v427.py \
  tests/test_ui_delivery_gate_v492.py tests/test_transition_exhaustive_v485.py \
  tests/test_output_styles_v47.py
# 75 passed / 0 failed，退出码 0

# 3. B7 工作区独立复核（当前最终页面）
cd _bench_workspaces/b7-shopbench-ours && PORT=4178 node server.js
node services/api/scripts/browser_probe.mjs \
  --url http://127.0.0.1:4178/ --steps data/b10-nowait-steps.json \
  --shot data/b10-nowait-shot.png --shot-open data/b10-nowait-open.png \
  --wait 1000 --viewport 1440x900
# ok:true；8/8 步通过；step_failed=0；console 0 错误；页面文本含商品档案、
# 实时状态、技术参数、故障码；点击「立即巡检」后无 wait 断言即通过

# 4. check.sh 等价逐条（--no-smoke；本机无 bash）
cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 443 tests / 442 pass / 1 fail，退出码 1
# 第一条失败：transition-copy.test.ts:124
#   「后端每一个 rule 都必须在本文件登记」
#   actual ['list_only', 'ui_repair_reserve'] vs expected []
```

- B7 运行证据：`data/b7-run.json` 任务 `c328c027-...`，状态 `succeeded`，
  28 步 / 32 次模型调用 / 1,757,693 token / 16 次变更 /
  `npm test` 17 passed / browser_check 2 次 / 验收 3/3。

### 复核边界

- **本次门禁验收不通过**：前端单测 1 条红。原因是工作区未提交的
  `services/api/src/blank_api/services/agent/transition.py` 新增了
  `list_only` / `ui_repair_reserve` 两个跳转 rule，但前端
  `transition-copy.ts` 未登记；B10 未改这两个文件，这不是 B10 引入的回归。
  修复需由对应改动归属方确认，并重新跑前端单测。
- `data/b7-steps.json` 是 B7 早期页面版本的步骤（2026-11-22 21:27），
  与 B10 复跑出的最终页面结构不匹配（8/13 步失败：`button[data-mode]`、
  `#chat-log` 等元素不存在）；本次另写 `data/b10-nowait-steps.json`
  针对当前页面复核，8/8 通过。
- AI 巡检的独立慢接口复核未做；默认 settle 600ms 对慢接口可能不够
  （交接单已声明）。
- 完整后端 5000+ 未重跑（B10 交接单已声明）。
- B7 成功 run 的 `resultSummary` 自述「需要你：按阈值分档改写…」
  「这一轮有内容用的是占位值（example.com）…换成你的真实信息才算完成」，
  但任务状态是 `succeeded`，与 C1「交付未完成不再判成功」口径存在张力；
  需单独立项确认，不在 B10 范围内改判定。
- 只验证 Node 服务 + Chromium；没有 Claude/Codex 对照。

### 2026-11-23 复跑补记（X1）

- 在 `web/src/components/tasks/transition-copy.ts` 补登记 `list_only` /
  `ui_repair_reserve` 后，前端单测复跑 **443 passed / 0 failed**（退出码 0），
  `tsc --noEmit` 退出码 0，界面 journey 仍为退出码 0 且结论行正确。
- 上面的原始失败项保留不回填；B10 的门禁阻断已消除，等待人工最终确认。

## 2026-11-23 成本闸设置入口（Y1，人工验收）

### 范围

- 把已存在的金额闸（`services/agent/cost.py`：token 折价、默认关、分三段计价）
  与价目表接进 `GET/PUT /settings/agent` 和设置页；不改计价逻辑。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_settings_budget_v54.py tests/test_cost_budget_v427.py tests/test_settings_v13.py
# 30 passed，退出码 0

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 447 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 金额闸默认仍是关（`cost_limit = 0`）：本卡只提供入口，不会自动替用户省钱。
- 价目表用 JSON 文本域编辑；未做表格化与供应商预置（不编价格）。
- 币种未做：`normalize` 只保留 input/cached_input/output，`estimate_cost` 的
  currency 恒为空，这是既有行为，本卡未改。
- 未跑完整后端 5000+；本卡相关 30 条通过。

## 2026-11-23 产物面板增强（B3a，人工验收）

### 范围

- 产物视图：按路径搜索、按扩展名分组（页面/代码/数据/图片/文档/其他）、
  复制路径、导出 Markdown/JSON 清单。纯前端，不新增 API。

### 执行命令与结果

```bash
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 452 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 只做产物清单与导出，不做内嵌预览（B3b）与多文件应用预览（B3c）。
- 导出只含路径与变更类型，不含 diff；diff 仍以文件与证据包为准。
- 复制路径依赖 `navigator.clipboard`，非安全上下文会静默失败。
- 未跑完整后端；本卡未改后端。

## 2026-11-23 单文件内嵌预览（B3b，人工验收）

### 范围

- 文件预览按扩展名分流：HTML 用 `sandbox=""` 静态渲染、Markdown 复用现有渲染器、
  JSON 格式化（失败显示原文 + 原因）、CSV/TSV 转表格（引号/转义/CRLF、200 行 × 50 列上限）、
  其他文本保持 `<pre>`；截断文件显示提示。

### 执行命令与结果

```bash
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 458 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- HTML 预览不执行脚本、不加载相对资源；完整交互留给 B3c 多文件应用预览。
- CSV 解析是预览级状态机，不覆盖所有 RFC 4180 边界；超限标记截断。
- Markdown 链接仍走 `safeLinkHref` 白名单。
- 未跑完整后端；本卡未改后端。

## 2026-11-23 多文件应用预览（B3c，人工验收）

### 范围

- 按需启动工作区 `package.json` 里 `scripts.start` 为 `node <文件>` 的长驻服务：
  随机回环端口、环境变量白名单、Windows Job Object、10 分钟 TTL、进程树回收。
- `POST/GET/DELETE /api/v1/projects/{id}/preview`；前端「预览」Tab 接真实会话。
- 部署要求只准容器（`BLANK_SANDBOX_ALLOW_LOCAL=0`）时返回 409，不退回宿主裸跑。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_preview_v55.py tests/test_ui_delivery_gate_v492.py
# 22 passed，含真实 node 启动/复用/停止

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 461 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

# OpenAPI 检查：129 paths，含 /api/v1/projects/{project_id}/preview
PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 只支持 `node <文件>` start；不执行 npm/任意 shell；不读 `PORT` 的服务会启动超时。
- Job Object 只强制内存/进程数/生命周期；文件与网络边界未由系统强制，界面如实标注。
- 预览 iframe 用 `allow-scripts allow-same-origin allow-forms`，源是独立回环端口，
  脚本碰不到主应用存储；服务本身仍拥有当前用户权限。
- 预览会话按项目复用，10 分钟 TTL；关闭面板不会立即停止（有停止按钮）。
- 未做真实浏览器点击验收（见交接单的人工步骤）；未跑完整后端。

## 2026-11-23 结果状态条（U1，人工验收）

### 范围

- 过程摘要行增加状态词（正在做/完成/失败/已停止/等你批准/已暂停/已结束），
  摘要分段统一为「看了 N · 改了 N 个文件 · 跑了 N 条命令 · N 次失败」，失败标红。
- 不改工具调用行化；现状已是每步一行、可展开参数与输出。

### 执行命令与结果

```bash
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 464 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 状态词依赖后端状态名；未知终态显示「已结束」，不冒充完成。
- 摘要计数仍来自前端 `turnEvidence`，与右侧证据包口径可能不同（既有行为）。
- 未跑完整后端；本卡未改后端。

## 2026-11-23 自学习可见闭环（K1，人工验收）

### 范围

- 经验（学世界）与偏好（学人）的快照、逐条退役、候选偏好采用、体检告警。
- 设置页「记忆 → 自学习」展示；不改变自动召回/注入策略，技能提案仍人工采用。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_learning_v56.py --maxfail=1
# 5 passed

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 467 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

# OpenAPI 检查：133 paths，含 4 个 /learning 端点
PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 退役逐条进行，没有「一键清空」；退役不是删除，条目仍留在文件里。
- 「采用」只对 `origin=harvested` 且 `candidate` 的偏好显示；冲突/已退役不可提升。
- 项目层与全局层按指纹分别写回，项目层优先。
- 未做真实浏览器点击验收（见交接单人工步骤）；未跑完整后端。

## 2026-11-23 会话授权撤销（K2，人工验收）

### 范围

- 列出本会话有效授权、按类别撤销、全部撤销；会话条「授权 N」入口。
- 授权仍只从审批卡写入；撤销只影响未来判定，正在执行的调用不受影响。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q \
  tests/test_session_grants_v57.py --maxfail=1
# 4 passed

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 469 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

# OpenAPI 检查：135 paths，含 /threads/{id}/grants 与 /{category}
PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 授权列表只在有授权时显示；0 条不占位置。
- 会话条在最后一条轮次状态变化后刷新授权；刚批准时可能需要再点开一次。
- 存储文件损坏时 fail-closed：一条授权都不认。
- 未做真实浏览器点击验收（见交接单人工步骤）；未跑完整后端。

## 2026-11-23 液态玻璃皮肤 v1（D5，视觉草案）

### 范围

- 全局液态玻璃令牌（深色默认 / 浅色可切换）、极光背景与光晕、玻璃面板、
  输入框/滚动条/按压反馈；主框架、共享 Panel、输入区、右侧结果面板、
  团队/工作台左栏、会话条、待审批区、文件预览、Diff 文件树统一。
- 不改功能与数据；默认主题改为深色，已有 `blank.theme=light` 的用户保持浅色。

### 执行命令与结果

```bash
cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 469 passed / 0 failed

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在

# 浏览器探针：
# 首页（深色）    console 0 -> data/liquid-glass-dark-home2.png
# 设置（深色）    console 0 -> data/liquid-glass-dark-settings3.png
# 会话（深色）    console 0 -> data/liquid-glass-thread.png
# 首页（浅色）    console 0 -> data/liquid-glass-light-final.png
```

### 复核边界

- 视觉草案：技能、监控、证据库、Reverse Lab、头脑风暴、项目页、日常页尚未逐一截图复核。
- 浅色主题对比度偏淡，下一轮精修。
- 弹窗/菜单/Toast/表格未逐个重绘，目前靠语义令牌继承。
- 页面「丝滑程度」优化未开始。
- 未跑桌面打包；主题引导脚本已移除，打包前仍需重新构建以更新 CSP 哈希。

### 修订（同日）：只保留浅色 + 质感增强

- 按用户反馈移除深色主题、`data-theme` 切换与 `theme-toggle.tsx`，全站只保留浅色液态玻璃。
- 玻璃增加 SVG 细颗粒、内层折射、底部厚度阴影；主操作按钮改为彩色釉；背景极光加强。
- 复核：前端 **469 passed**、tsc/eslint 0、journey 通过；首页/设置/会话浏览器探针 **console 0**。
- 截图：`data/liquid-glass-light-v6.png`、`data/liquid-glass-light-settings3.png`、
  `data/liquid-glass-light-thread.png`。

### 修订二（同日）：侧栏清晰度与目录选择框

- 侧栏改用 `glass-side`：底色更实、无噪点，11–13px 小字清楚。
- 移除 `body > * { position:relative; z-index:1 }`：它会覆盖 portal 的 `fixed z-50`，
  导致「选择目录」打不开；光晕改为 `z-index:-1`。
- 目录选择框复核：点「选择目录 → 选择目录…」后 `[role=dialog]` 断言 **true**，
  截图 `data/liquid-glass-dir-picker2.png`。
- 复跑：前端 **469 passed**、tsc/eslint 0、journey 通过；首页 **console 0**，
  最终首页截图 `data/liquid-glass-light-v7.png`。

## 2026-11-23 全页面玻璃一致性 + 丝滑 v1（D6，人工验收）

### 范围

- 12 个页面逐页浏览器审计：首页、任务、设置、团队、技能、证据库、监控、项目、
  Reverse Lab、日常、头脑风暴、功能中心。
- 任务页补外层玻璃卡片；会话新增「回到底部」、加载骨架屏、页面入场动画。

### 执行命令与结果

```bash
# 逐页 browser_probe（1440×900，每页截图到 data/glass-audit/）
# 结果：12/12 ok=true，console 0，无失败请求

# 回到底部（1280×600 长会话）
# 滚到顶 -> 按钮出现 -> 点击 -> 距底 <120px
# steps 8/8 通过，截图 data/liquid-jump-bottom2.png

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 469 passed / 0 failed，退出码 0

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 页面级轮询/SSE 未重构；审批等态仍 2.5 秒拉一次。
- 骨架屏只覆盖会话加载；其他页面空态未逐个细化。
- 未做真实低端设备的 backdrop-filter 性能测量。
- 未跑完整后端；本卡未改后端。

## 2026-11-23 结果面板重设计（D7，开发自测）

### 范围

- 普通对话没有产物/改动/证据时，右侧结果面板与顶部入口不显示。
- 右侧面板重设计：默认 620px（420–900 可拖、双击复位）、四视图固定入口
  （产物/改动/文件/预览）、文件标签独立一行、工具栏只留路径/文件数/状态/复制。
- 代码 diff 与文本/JSON 预览改横向滚动，长行不再强制换行，无需全屏也能看全。

### 执行命令与结果

```bash
# 浏览器探针：无产出会话（「你好」）
# 断言：不存在面板 separator -> true；console 0
# 截图 data/panel-no-output.png

# 浏览器探针：有产出会话（shopbench 真实复跑）
# 断言：面板存在 -> true
# [data-diff-scroll] 实测 scrollWidth 961 / clientWidth 444 / scrollLeft 200
# 断言：可横向滚动 -> true；console 0
# 截图 data/panel-redesign-thread-clean.png

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 469 passed / 0 failed

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0

PYTHONUTF8=1 ... python scripts/acceptance.py \
  --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
# 退出码 0；结论：通过：打开有东西，动作有变化，刷新还在
```

### 复核边界

- 未改后端；面板数据来源与轮询逻辑未动。
- 面板宽度已持久化；视图与标签页偏好刷新后不保留。
- 未跑完整后端；本卡未改后端。

### 补记：横向拖动后行底色消失

- 反馈：代码行底色只在初始可视区显示，拖到右边就没有了。
- 根因：每行宽度是自身 `max-content`（最短也有 `min-width:100%`），短行只有 444px，
  横向滚动后其底色随之移出视口。
- 修复：行容器 `w-max min-w-full`、每行 `w-full`，所有行底色铺满最宽行的 961px；
  路径栏加 `left-0` 横向粘住。
- 复验：`scrollLeft 400` 时行宽全部 **961px**，命中位置带新增/删除底色，断言 true；
  截图 `data/d7-bg-fixed.png`（修复前对照 `data/d7-bg-scrolled2.png`）。
- 复跑：前端 **469 passed**、tsc/eslint 0。

## 2026-11-23 界面状态记忆（D8，开发自测）

### 范围

- 侧栏收窄状态记忆：收起后刷新、跨页回来仍保持。
- 结果面板开合状态记忆：收起后刷新仍收起，标题栏按钮可重新打开。
- 会话滚动位置记忆：同一次打开内切走再切回，回到离开时的位置；原本在底部则继续贴底。
- 结果面板状态按会话隔离：切换会话时文件标签、搜索、视图重置。

### 执行命令与结果

```bash
# 浏览器探针（1440×900）：侧栏 / 面板
# 点「收起会话列表」-> reload -> 「展开会话列表」存在：true
# 点「收起面板」-> 无宽度分隔条：true -> reload -> 仍无分隔条 + 「打开结果面板」存在：true
# 点「打开结果面板」-> 分隔条恢复：true
# console 0，失败步骤 0；结果 data/d8-memory-result.json

# 浏览器探针（1440×520）：滚动位置
# 长会话 scrollTop=300（可滚范围 514）-> 切到「你好」-> 切回 -> top=300
# 断言：true；console 0；结果 data/d8-scroll-result2.json

cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 469 passed / 0 failed

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0
```

### 补记：面板文件标签跨会话残留

- 现象：两条都有产出的会话之间切换时，结果面板不卸载，上一条会话打开的文件标签
  留在新会话里，点开是「文件读取失败」。
- 修复：`TaskPanel` 加 `key={conv?.thread.id}`，按会话重挂载，文件标签/搜索/视图/全屏
  随会话重置。
- 复验：shopbench 会话打开 `evidence/v0.11.2-final/dd-01-home.png` 文件标签（存在 true）
  → 切到「前端闸门验证 3」→ 该标签不存在 true；console 0；截图 `data/d8-panel-tabs.png`。

### 复核边界

- 滚动位置不跨刷新保存（只在本次打开内）。
- 面板视图/标签页偏好未记忆；面板宽度原本已持久化。
- 未跑 journey 与后端；本卡只改前端 `bot-app.tsx`。

## 2026-11-23 执行环境与目标状态修复（E1，开发自测）

### 背景

用户反馈「任务做不完、不停报错」。查 `blank_dev.db` 任务 8f2f7734（写项目计划书）：
32 步里有 9 条命令失败，根因是三处——

1. `normalize_python_command` 把 `py -3` 改写成应用自带的精简解释器，得到
   「Unknown option: -3」，模型据此以为机器上没有可用 Python；
2. 该解释器没有 pip，`python -m pip install python-docx` 必然失败，回执没有解释；
3. cmd.exe 里用了 `| tail` / `| head`，命中 Git 的 POSIX 版本，报错难懂；
   工具说明还教模型「用 ; 串联命令」，而 cmd 不认 `;`。

同一任务最终改用标准库 zipfile 完成（succeeded / 32 步），但失败噪声本可避免。

### 修复

- `py -3` / `py -0p` / `py -V` 原样保留；只有 `py -m 模块` 换成项目 venv。
- 解释器缺 pip 时回执直接给出 `py -3 -m pip` / 项目 venv / 标准库三条路。
- cmd 下误用 POSIX 文本管道时，回执给出重定向 + read_file 的替代做法。
- run_shell 说明按真实 shell 生成：cmd 下用 `&&` / `dir`，明确 `;` 不是分隔符。
- 恢复执行时把目标从 `waiting_approval` 拉回 `executing`（状态条不再假「等你批准」）。
- 编程场景提示：文档默认 Markdown；不反复装依赖，失败两次就停。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_legacy_fixes_v162.py tests/test_shell_launcher_hints_v411.py \
  tests/test_shell_dialect.py tests/test_shell_decode_v149.py \
  tests/test_cmd_semicolon_v410.py -q
# 53 passed

.venv/Scripts/python.exe -m pytest \
  tests/test_scenarios_v153.py tests/test_two_scenes_v401.py \
  tests/test_doing_tasks_prompt_v167.py tests/test_prompt_parts_v427.py \
  tests/test_output_styles_v47.py -q
# 75 passed

.venv/Scripts/python.exe -m pytest tests/test_goal_revive_v411.py -q
# 2 passed
```

真实命令复验：
- `py -3 -c "import sys; print(sys.executable)"` → 退出码 0，输出 Python312；
  `py -3 -m pip --version` → pip 25.0.1。
- `py -m pytest` 仍替换为 `.venv/Scripts/python.exe -m pytest`。
- `<venv python> -m pip install python-docx` → 退出码 1，pip 缺失提示触发。
- 后端已重启，`/api/v1/health` 200；首页浏览器探针 console 0、无失败请求。

### 复核边界

- 未给运行时补装 pip；仍建议 `py -3` 或项目 venv。
- 未改断路器阈值；同签名/同类失败判定沿用 `stall.py`。
- 提示词改动从下一轮对话生效；历史任务记录不追溯。

### 补记：完全访问的审批边界与 500 文案

- 完全访问只免低/中风险；高风险（装依赖、内联脚本、删除、push、部署、迁移、
  工作区外、联网）永远问一次。本次唯一的审批卡 `risk_level=high`，命令含
  `pip install` 与 `python -c` 两条高风险特征；批准后同类不再逐条问。
- 发送栏常驻提示补上「首次装依赖、内联脚本（如 python -c）」。
- 「请求失败（HTTP 500）」是无结构化错误体的兜底文案：后端 500 一定带 JSON
  消息；该形状对应开发态 Next rewrite 在后端不可达（重启窗口）时的 HTML 500。
  现在这类 5xx 显示「后端暂时不可用…可能正在重启」，并附请求方法与路径。
- 复跑：前端 **473 passed**、tsc/eslint 0。

## 2026-11-23 开局环境预检（E2，开发自测）

### 范围

- 任务开局探测：python / `py -3`（含 pip）/ 工作区 `.venv`（含 pip）/ node / git / uv。
- 结果压成一段「本机环境（开局实测）」注入系统提示；只报版本与存在性，不报路径；
  明确写「不要再用 where / --version 逐条探测」。
- 按工作区 + PATH 缓存 5 分钟；纯聊天 / 无工具任务不探测；失败只记 warning。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_env_profile_v412.py -q
# 5 passed

.venv/Scripts/python.exe -m pytest \
  tests/test_env_profile_v412.py tests/test_goal_revive_v411.py \
  tests/test_doing_tasks_prompt_v167.py tests/test_prompt_parts_v427.py \
  tests/test_output_styles_v47.py -q
# 53 passed（含真实 runtime 构建 initial state 的路径）

# 本机实测 profile() 耗时与输出
# elapsed_ms 833
# py -3: 3.12.10（pip 25.0.1 可用）
# .venv: 3.12.14（没有 pip）
# node 24.20.0 / git 2.56.1.windows.1 / uv 未安装
```

后端已重启，`/api/v1/health` 200；首页浏览器探针 console 0、无失败请求。

### 复核边界

- 未自动装包；没有 pip 时只给「别装、用标准库」的结论。
- 未探测 npm/pnpm 版本。
- 缓存 5 分钟内的环境变化不会反映到提示里；提示措辞限定为「开局实测」。

## 2026-11-23 长文件分段写入（E3，开发自测）

### 范围

- `write_file` 新增 `append`：追加到已有文件末尾（不存在则新建），不覆盖原内容。
- 接缝：追加片段去 BOM、按原文件换行风格归一、原文无尾换行时补一个；
  空追加判「未变化」；二进制文件拒绝追加。
- 工具说明与编程场景提示：单次 content 超过约 4000 字时分节写，
  用 `write_file(append=true)` 追加，避免参数流中断导致整次失败。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_append_write_v412.py tests/test_claude_parity_v24.py \
  tests/test_edit_tolerance_v424.py -q
# 82 passed

.venv/Scripts/python.exe -m pytest \
  tests/test_append_write_v412.py tests/test_env_profile_v412.py \
  tests/test_goal_revive_v411.py tests/test_scenarios_v153.py \
  tests/test_two_scenes_v401.py tests/test_prompt_parts_v427.py -q
# 48 passed

# 工具 schema：append in properties = True；description 含「分段」
```

后端已重启，`/api/v1/health` 200；首页浏览器探针 console 0。

### 复核边界

- 追加仍受「读后写」闸门约束（已有文件必须先读过）。
- 分段由模型决定，不做自动切片；追加实现为整份重写，超大文件不适合。

## 2026-11-23 环境预检补记：文档库

- 同一次开局探测顺带读 `python-docx` / `openpyxl` / `python-pptx`。
- 提示渲染：已装则写「文档库已装: py -3: python-docx」；未装则写
  「要用标了 pip 可用的解释器装一次（首次需审批），否则用 Markdown/CSV 交付」。
- 本机实测：py -3 有 pip 25.0.1、三个文档库均未安装；预检 702ms。
- 单测：`tests/test_env_profile_v412.py` 5 passed。

## 2026-11-23 文档库随包预装（E4，开发自测）

### 范围

- `python-docx / openpyxl / python-pptx` 纳入 `services/api/pyproject.toml`，
  `uv.lock` 更新（79 packages）。
- 开发 venv 与 `desktop/build/runtime` 均已安装；构建脚本的冒烟导入加入三库。
- 环境预检新增「应用解释器」（`sys.executable`，即 run_shell 改写后的解释器）
  并报它的文档库；单进程超时 3s→6s（冷启动 import lxml/Pillow 实测超 3s）。

### 执行命令与结果

```bash
cd services/api && uv lock            # Resolved 79 packages
uv lock --check                       # 退出码 0
uv pip install --python .venv/Scripts/python.exe \
  python-docx==1.2.0 openpyxl==3.1.5 python-pptx==1.0.2
# Installed 7 packages

.venv/Scripts/python.exe -c "import docx, openpyxl, pptx"   # 1.2.0 / 3.1.5 / 1.0.2
# 生成实测：a.docx 36633 字节 / a.xlsx 4828 / a.pptx 28213

node scripts/prepare_desktop_runtime.mjs
# ✓ desktop/build/runtime  包数 69  体积 153.3MB（冒烟导入含 docx/openpyxl/pptx）
desktop/build/runtime/python.exe -c "import docx, openpyxl, pptx"   # 成功

cd services/api && .venv/Scripts/python.exe -m pytest tests/test_env_profile_v412.py -q
# 5 passed

# 预检实测新增：
# - 应用解释器: 3.12.14（没有 pip）
# - 文档库已装: 应用解释器: python-docx、openpyxl、python-pptx
```

后端已重启，`/api/v1/health` 200；首页浏览器探针 console 0。

### 复核边界

- 未重新打 Electron 安装包；`desktop/dist` 旧产物不含新库，下次 `npm run dist` 生效。
- 旧运行时留档为 `desktop/build/runtime-old-<时间戳>`（约 129MB）。
- 只覆盖 Word/Excel/PPT 三库，不含 pandas 等数据科学库。

## 2026-11-23 文档产物预览（E5，开发自测）

### 范围

- 结果面板打开 docx / xlsx / pptx：后端抽取文本层（标题、段落、表格、工作表、
  幻灯片文字），按 markdown 返回；前端复用 Markdown 渲染器，并显示
  「Word 文档 · 94 段 · 12 个表格；文本预览，样式与图表已简化」。
- 抽取上限：20,000 字、3 个工作表、30 行 × 12 列、40 页、2,000 段。
- 坏文件/加密文件抽不出来时，保持原「二进制文件」回执。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_office_preview_v413.py -q
# 5 passed（docx/xlsx/pptx/坏文件/扩展名）

GET /api/v1/projects/29044bb7.../file?path=数据智能分析平台-项目计划书.docx
# 200；binary=false，preview=office，content 15277 字

# 前端
node --test ... "src/**/*.test.ts" scripts/csp.test.mjs   # 473 passed
tsc --noEmit / eslint src --max-warnings=0                # 退出码 0
```

浏览器探针（1440×900，商品计划书会话）：
- 打开结果面板 → 文件 → 点击 docx；
- `hasOfficeBanner=true`、`hasTitle=true`、`binaryMessage=false`；断言通过，console 0。
- 截图 `data/e5-office-preview.png`。

### 复核边界

- 只做文本层预览，不还原样式、图表、公式、图片；要原貌仍需用系统应用打开。
- 表格以 `text` 代码块呈现（前端 Markdown 渲染器不支持表格语法），列宽不保证对齐。
- `read_file` 工具读数 Office 文件仍是二进制回执，本卡只服务结果面板。

## 2026-11-23 国内模型默认与包镜像（E6，开发自测）

### 范围

- 默认供应商改为设置里显式锁定，并切到 **DeepSeek**（`default_provider`）。
- 供应商列表新增 `is_default`；前端「默认模型」按它显示，不再按列表第一项猜。
- 子进程默认使用国内包镜像：pip / uv → 清华 PyPI，npm → npmmirror；
  显式配置不覆盖，`BLANK_DOMESTIC_MIRRORS=0` 可关闭。
- 办公文档交付默认 `.docx`（WPS/Office 可直接打开），代码仓库说明仍用 Markdown。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_provider_default_flag_v414.py tests/test_domestic_mirrors_v414.py \
  tests/test_default_provider_v427.py -q
# 13 passed

.venv/Scripts/python.exe -m pytest \
  tests/test_handoff_v12.py tests/test_effort_batch.py tests/test_scenarios_v153.py -q
# 66 passed

# 子进程环境实测
# PIP_INDEX_URL / UV_INDEX_URL = https://pypi.tuna.tsinghua.edu.cn/simple
# npm_config_registry = https://registry.npmmirror.com

GET /api/v1/providers
# deepseek 的 is_default=true

# 前端
node --test ... "src/**/*.test.ts" scripts/csp.test.mjs   # 475 passed
tsc --noEmit / eslint src --max-warnings=0                # 退出码 0

# 浏览器探针：模型选择器显示 deepseek-flash；console 0
```

### 复核边界

- 默认供应商写在本机 `data/settings.json`；换机需重新选择（设置页暂无默认供应商入口）。
- 镜像只作用于 Agent 子进程，不影响应用自身请求。
- 办公文档默认 `.docx` 只影响办公类交付，不改代码仓库说明。

## 2026-11-23 任务筛选状态进地址栏（D9，开发自测）

### 范围

- 任务页搜索词与状态筛选写入 URL（`q` / `status`），`replaceState` 不产生历史记录，
  保留 `project` 等其他参数；非法状态、空白搜索词读回时忽略。
- 刷新、收藏、从详情返回后筛选仍在。

### 执行命令与结果

```bash
cd web && node --test --test-concurrency=2 --import ./test-bootstrap.mjs \
  "src/**/*.test.ts" scripts/csp.test.mjs
# 481 passed / 0 failed（含 filters.test.ts 5 条）

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0
```

浏览器探针（/tasks）：
- 输入「计划书」→ 点「未成功」→ URL `?q=计划书&status=failed`；
- reload → 输入框仍是「计划书」、选中仍是「未成功」；断言通过，console 0。
- 截图 `data/e8-task-filters.png`。

### 复核边界

- 不做跨标签页同步；读回在首帧之后（避免 hydration 不一致）。
- 不动任务列表轮询与分组。

### 补记：任务页左下角模型显示

- 现象：任务页左下角显示 `claude-sonnet-5-5`（第一个启用项），实际默认是 DeepSeek。
- 修复：改认 `defaultProviderId()`（有 Key + is_default）。
- 复验：浏览器探针 `hasDeepseek=true / hasClaude=false`；截图 `data/e8-task-filters-footer.png`。

## 2026-11-23 结果面板文件条上移（D10，开发自测）

### 范围

- 顶部四个视图标签（产物/改动/文件/预览）按面板实际宽度取舍：<640px 隐藏数量，
  <420px 再隐藏文字；统一 `whitespace-nowrap`，不再把中文挤成竖排。
- 「改动」视图的文件列表从左侧竖排移到代码区上方，横向文件条可滚动；
  溢出时出现左/右箭头按钮；从产物/文件标签切换时选中项自动滚入视野。
- 树形模式保留：文件条按目录分组。

### 执行与结果

```bash
cd web && node --test ... "src/**/*.test.ts" scripts/csp.test.mjs   # 481 passed
cd web && tsc --noEmit / eslint src --max-warnings=0                # 退出码 0
```

浏览器探针（面板宽 460，真实 3 文件会话）：
- 四个标签均为 **62×28** 单行；文件 chip 在 diff 上方（bottom 263 ≤ diffTop 270）；
- 点右箭头 → `scrollLeft=36` 且出现左箭头；点左箭头 → 回到 0；断言全部通过，console 0。
- 截图 `data/e9-panel-top-files.png`、`data/e9-file-arrows.png`。

### 复核边界

- 箭头只在溢出时出现；树形目录标签不可点击。
- diff 横向滚动与底色逻辑未改（D7）。

## 2026-11-23 结果面板精简（D11，开发自测）

- 删除改动视图三层工具栏：面板级「改动 / 3 文件 / 完成」行、
  「本会话 / 搜索文件 / 分组」行、「变更 / 证据 / 活动」分段。
- 改动视图现在从顶部四个视图标签直接进入文件条 + diff；其他视图保留标题/路径/状态/复制。
- 验证：前端 **481 passed**、tsc/eslint 0；浏览器探针断言无「改动范围」下拉、
  无「搜索文件」、无三个分段，文件条与 diff 存在，产物↔改动切换正常，console 0。
- 截图 `data/e10-panel-clean.png`。证据/活动在对话面板内不再可达（任务页仍有）。

## 2026-11-23 日常场景对标 Codex（E8，开发自测）

- DAILY 提示词收紧四条：直接动手少问（一次只问一个）、先用最简单可靠的做法、
  做完必须核对结果、交付先给结论；涉及代码/页面仍要真跑验证；默认简体中文。
- 新对话默认场景改为「日常」：`scenarioKey` 初始值 `dev` → `daily`，存过的选择仍优先。
- 验证：`pytest test_scenarios_v153.py test_doing_tasks_prompt_v167.py` **24 passed**；
  前端 **481 passed**、tsc/eslint 0；新会话场景选择器显示「日常」，console 0。
- 截图 `data/e11-daily-default.png`。后端 `DEFAULT_SCENARIO` 仍为 dev（API 回落不变）。

## 2026-11-24 会话批量归档（T-BA，开发自测）

### 范围

- 首页会话侧栏「管理」进入选择态：活跃会话出现复选框、行尾菜单隐藏，
  底部操作条显示「已选 N 条 / 全选 / 取消 / 归档所选（N）」；Escape、取消、
  侧栏收起、归档成功都会退出管理态并清空选择。
- 一次整批确认后只发一次 `POST /api/v1/threads/batch-archive`，响应分
  `archived / unchanged / missing` 三桶；归档只改 `status`，不停止任务、不删轮次。
- 侧栏新增「已归档 N」折叠组（默认收起、搜索时自动展开）；已归档行菜单改为
  「取消归档」，取消后回到活跃分组。
- `GET /threads` 与 `GET /projects/{id}/threads` 新增
  `status=all|active|archived`，**先过滤再 limit**；首页、全站左栏、任务页会话
  选择器只取活跃会话。

### 执行命令与结果

```bash
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_threads.py -q
# 28 passed（含状态筛选先过滤后 limit、批量归档三桶、幂等、去重、空/超限 422、
# 归档不停止运行中任务）

cd services/api && .venv/Scripts/python.exe -m pytest \
  tests/test_thread_status_v151.py tests/test_thread_rewind_v117.py -q
# 14 passed

cd web && node ./node_modules/typescript/bin/tsc --noEmit          # 退出码 0
cd web && node ./node_modules/eslint/bin/eslint.js src --max-warnings=0  # 退出码 0
bash check.sh --no-smoke
# [PASS] tsc / eslint / node --test（495 passed）/ journey
# 全部通过。exit=0
```

权威 journey（`scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html`）：

- 断言 **15 条**全过（首屏 3 条 + 旅程 12 条）：首屏 3 张卡片、2 列；添加/拖拽/
  刷新/过滤动作逐条变化；
- `结论：通过：打开有东西，动作有变化，刷新还在`，exit=0。

浏览器探针（真实前后端，`http://127.0.0.1:3000`）：

- 1440×900：26 个步骤全过——管理入口 → 勾选 1 条 → 底部显示「已选 1 条」→
  整批确认（探针注入 confirm）→ 只发一次 batch-archive → 成功提示
  「已归档 1 条会话，可在「已归档」中找回」→ 已归档组出现 → 展开 → 菜单
  「取消归档」→ 回到活跃列表 → 搜索只剩 1 条时全选只选 1 条 → Escape 退出；
  console 0，失败请求 0。
- 1024×768：管理态、勾选、操作条按钮完整可见，`aside.scrollWidth <= clientWidth`，
  取消后复选框清空；console 0。
- 真实接口复核：归档后 `GET /threads?status=active` 不含该会话、
  `GET /threads?status=archived` 含该会话；验收项目与会话已在结束后删除。
- 截图：`data/batch-archive-managing-1440.png`、
  `data/batch-archive-archived-1440.png`、`data/batch-archive-1440.png`、
  `data/batch-archive-1024.png`。

### 复核边界

- 未发真实聊天消息验证「取消归档后继续聊」：接口层 `thread_archived` 409 逻辑
  未改，且取消归档后 `status=active` 已由接口与浏览器断言确认；人工验收可补一次。
- `check.sh --no-smoke`：未跑端到端冒烟（会调用真实模型）；其余门禁全跑。
- 已归档组最多展示 200 条；超出部分本卡不解决。

