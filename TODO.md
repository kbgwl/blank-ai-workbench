# TODO

> 当前按用户指令开发 **NEXT-DEV-v5**（见 dev/NEXT-DEV-v5.md）。

## 进行中 / 待验收

- [ ] **ARCH-01b：运行失败与通知决策外移** — **READY_FOR_QA**
  - 范围：把 `_record_run_failure`、`_graph_decision`、
    `_resume_decision_delivered`、`_notify_kind`、`_origin_from_trigger`、
    `_record_notify_decision` 移到 `services/orchestration/runner.py`；
    `_execute_claimed_run` 暂留 tasks.py。
  - 验证：导入检查通过；通知/租约/worker/触发器 **61 passed**；全量
    （排除 3 项 browser 环境红）**5164 passed / 24 skipped / 3 deselected**
    （21:06）。
  - 未做：`_execute_claimed_run` / `LEASE_HEARTBEAT_SECONDS` 仍在 tasks.py。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-ARCH-01b-claude.md`
- [ ] **ARCH-01a：tasks.py 第一批领域动作外移** — **READY_FOR_QA**
  - 范围：新增 `services/orchestration/runner.py`，把 `persist_task` /
    `_record_dispatch` 从路由层移出；`tasks.py` 保留同名导入，现有调用方不变。
  - 验证：导入检查通过；建任务/线程/worker 45 passed、创建/派发/触发/IM/
    计划模式 102 passed；全量（排除 3 项 browser 环境红）
    **5164 passed / 24 skipped / 3 deselected**（18:18）。
  - 未做：`launch_task` / `_execute_claimed_run` / `recover_*` 仍在 tasks.py。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-ARCH-01a-claude.md`
- [ ] **ARCH-05：worker 有界并发** — **READY_FOR_QA**
  - 范围：`worker.run_once()` 改 `asyncio.Semaphore(BLANK_WORKER_CONCURRENCY)`
    有界并发（默认 2）；抽出 `_run_candidate`，单条候选异常不带走同轮其他 run。
  - 验证：worker/调度 8 文件 **75 passed**；全量（排除 3 项 browser 环境红）
    **5164 passed / 24 skipped / 3 deselected**（20:12）。附带修复
    browser_probe 的 Windows 进程残留（清理后残留 0）。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-ARCH-05-claude.md`
- [ ] **ARCH-06b：开启 SQLite `foreign_keys=ON` + 测试夹具收口** — **READY_FOR_QA**
  - 范围：`db.py` 恢复 `PRAGMA foreign_keys=ON`；修 18 个测试文件的父子插入
    顺序与父行缺失；孤儿行用例改用独立 `sqlite3` 连接临时关外键造数。
  - 验证：已知外键子集 **212 passed**、孤儿行 **19 passed**、TypeError 组
    **8 passed**；清理残留 headless Chrome 后全量
    **5163 passed / 3 failed / 24 skipped**（3 项为 browser_check 环境红，
    `logs/pytest-fk-b-2026-11-27.log`）。
  - 未完成：真机级联删除验收。交接单
    `ops/HANDOFFS/T-LAUNCH-ARCH-06b-claude.md`。
- [ ] **MASTER-LAUNCH-PLAN Step 1 批次 1B：隔离读图 + Job 挂载竞态** — **READY_FOR_QA**
  - 范围：BUG-01 `fs_broker` 新增 `read_bytes`，`read_file` 在 broker/隔离下
    读图不再退化成文本或跳过读图；BUG-05 `LocalSandbox` 改
    `CREATE_SUSPENDED` → 挂 Job → 按线程恢复，消掉先起进程再挂 Job 的窗口。
  - 验证：定向 **27 + 170 + 25 passed**；全量 **5163 passed / 3 failed / 24 skipped**
    （3 项为 browser_check 提权临时目录环境红）。日志 `logs/pytest-1b-2026-11-27.log`。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-1B-claude.md`
- [ ] **MASTER-LAUNCH-PLAN Step 1 批次 1A：止血修复** — **READY_FOR_QA**
  - 范围：BUG-02 `FallbackAdapter` 透传输出预算；BUG-03 只读场景补齐
    `write_file` / `edit_file` / `notebook_edit`；BUG-04 AppContainer 属性号
    `0x20009`；ARCH-06 部分（SQLite WAL / `synchronous=NORMAL` / `busy_timeout`）；
    连带修复 `create_task` 会话/任务插入顺序与 `runtime._prepare` 空工厂告警。
  - 验证：定向 13 文件 **349 passed**；全量 **5162 passed / 3 failed / 24 skipped**
    （3 项为提权 pytest 临时目录 ACL 导致 browser_check 读不到页面，已留对照证据）。
  - 未做：BUG-01（broker 读图）、BUG-05（CREATE_SUSPENDED 挂 Job）、UP-03 测试归档、
    `foreign_keys=ON`（转 ARCH-06b）。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-1A-claude.md`
  - 检查点提交：`ae17635`
- [ ] **NEXT-DEV-v5 P0/P1/P2 收口 + P3 全量** — **READY_FOR_QA**
  - P0-1：推测执行对缺 `speculative` 属性的轻量会话兼容；6 个 stream 回归全绿。
  - P1-2：lsp 根因是 TEMP 在仓库内触发 `git check-ignore` 全量过滤；
    产品修 `_git_ignored` 根探测，测试加 `GIT_CEILING_DIRECTORIES`；lsp 40 passed、
    worktree 128 passed。
  - P1-3：提示词压轮次；真机 T09 **100 分 / 12 轮 / 283K / 46s**。
  - P1-4：推测执行开/关 T09 **46s vs 47s**，无回归、无可测墙钟收益。
  - P2：202 断言、worktree、output_styles、快照摘要与变更计数全部收口；
    `compare.mjs` 校验进程固定 UTF-8（修复一次 T09 假红 55→100）。
  - P3-12：最终 HEAD 全量 **5154 passed / 28 skipped / 0 failed**（20m28s）。
  - 已核对无需重做：T-A2 授权撤销、B3a/b/c 与产物条。
  - 未做：B3 新口径（缺具体范围）、竞品同批对照。
  - 交接单：`ops/HANDOFFS/T-R2-08-claude.md`
- [ ] **NEXT-DEV-v4 任务 3/4 + 测试清理第二批** — **验收：退回开发（任务4回归P1、lsp未修复、调用未达）**
  - 任务 3：钩子 fail-closed（`on_failure=block`）；`done_when` 逐条对账，
    `met` + 非空证据才放行；审查确定性范围（git base/head/merge-base +
    committed/staged/unstaged/untracked 四组路径，baseSha 开局取一次）。
  - 任务 4：推测性只读执行（`read_file/glob/search_code`，仅 bypass；
    结果只缓存，执行点仍走网关权限/记账；`file_state` 读记录延迟到真交付）。
  - 任务 5：`test_handoff_e2e_v12` / `test_unknown_facts_e2e_v147` /
    `test_unknown_kinds_e2e_v148` / `test_plan_mode_v113` / `test_prompt_submit_v123` /
    `test_stop_failure_hooks_v55` 审批断言改 202 + 等待后台恢复。
  - 未做：真机延迟对照；全量复跑。
  - 本轮按用户指示未跑测试。
  - 交接单：`ops/HANDOFFS/T-R2-07-claude.md`
- [ ] **NEXT-DEV-v4 任务 1b/1c + 测试清理第一批** — **验收：部分通过（1b/1c 调用≤12未达，测试清理部分完成）**
  - 1b：`parallel_write_tools`（不同文件 + bypass 并发写/改；同文件/需审批串行）。
  - 1c：提示词明确多文件同批、写后不重读。
  - T09 `expect_files` 方案 B：`tools/links.py` → `tools/`。
  - 测试清理：worktree 残留清理（93 passed）；审批 202 断言更新 5 个文件
    （34/17/27/2/3 passed）；lsp 单独 40 passed、组合 123 passed。
  - 验证：110 + 123 + 93 passed。
  - 未做：任务 2 lsp 全量复现、任务 3 质量底线、任务 4 速度、其余 202 断言/全量。
  - 交接单：`ops/HANDOFFS/T-R2-06-claude.md`
- [x] **NEXT-DEV-v4 任务 1a：工具说明书瘦身** — **验收通过（Codex，待人工验收 + minimal 扣分决策）**
  - 范围：常驻工具 **34→30**（Skill 市场四工具按需，`tool_search` 可拉回）；
    压缩 `run_shell` / `request_user_input` / `search_code` / `edit_file` /
    `update_plan` / `set_goal` / `task_update` / `propose_skills` /
    `report_findings` 的重复文案与参数说明。
  - 实测：常驻说明 **22,694→19,457 字符（-14.3%）**，T09 文档任务
    **11,291→10,354（-8.3%）**；`SCHEMA_BUDGET_CHARS` 22,700→**20,000**。
  - 验证：定向 **219 passed**。
  - 未做：真机 T09/T01 对照（需重启后端）；1b/1c、lsp、质量底线、速度、
    全量测试清理。
  - 交接单：`ops/HANDOFFS/T-R2-05-claude.md`
- [x] **NEXT-DEV-v3 任务 2：P1 修复（场景分流 + 慢扫描边界 + 系统缓存统计）** — **验收通过（Codex，待人工验收）**
  - 范围：普通任务只查工作区、整机检查需明确请求；`slow_scan_hint` 覆盖
    `%SystemDrive%` / `%SystemRoot%` / `%windir%` / `$env:SystemDrive`；
    `SnapshotStore` 扫描/复制/`changed_since` 排除系统缓存副本；
    `evals/compare.mjs` 同步忽略，修复 T09 `minimal=0`。
  - 验证：定向 **96 passed**、`node --check evals/compare.mjs` 0。
  - 未做：NEXT-DEV-v3 任务 1 的
    工具说明书瘦身 / 执行层合并 / 提示词强化。
  - 交接单：`ops/HANDOFFS/T-R2-04-claude.md`
- [ ] **NEXT-DEV-v2 任务二：压调用次数（提示词第一刀）** — **验收通过（判据部分达成）**
  - 范围：系统提示允许不同文件的独立写/改在自动放行档同一批发出；刚写过的
    文件编辑时直接用写入原文做 `old_string`，不要为找锚点重读。
  - 依据：`dev/T09-CALL-DIAGNOSIS.md`（3 次重读 + 3 次 write 分三轮 +
    3 次 edit 分三轮；目标 ≤12 次调用）。
  - 验证：定向 **338 passed**（提示词/集成/快照/编辑/交付/审查）；
    真机 T09 对照未跑（需确认后端已加载本次改动）。
  - 未做：工具说明书瘦身；写工具「不同路径并发执行」白名单（同批已按顺序跑，
    只省模型往返）。
  - 交接单：`ops/HANDOFFS/T-R2-03-claude.md`
- [ ] **NEXT-DEV-v2 任务一：文件状态快照取代历史回放** — **READY_FOR_QA**
  - 范围：`FileStateCache` 记录行数/最近读写；`render_snapshot()` 生成
    路径 + 行数/大小 + 最近操作 + 短指纹；旧 `read_file` 结果折成快照指针；
    token 模式低于 0.82 水位也做确定性瘦身（`micro:below`）；快照插到对话尾部。
  - 验证：定向 **338 passed**（27 个文件：快照/压缩/配对/编辑/交付/审查/
    Agent 集成），`git diff --check` 0。
  - 未做：真机 T09 / `evals/compare.mjs` 对照（未确认后端已加载本次改动）；
    「压调用次数」后续；快照的内容摘要与变更计数。
  - 交接单：`ops/HANDOFFS/T-R2-02-claude.md`
  - 调用诊断：`dev/T09-CALL-DIAGNOSIS.md`
- [x] **GOAL-ROADMAP-v2 第 1 步：写类工具只回执** — **验收通过（Codex 代做端到端人工验收）**
  - 范围：`edit_file` / `write_file` 的 outcome 不再带完整 diff，改带
    `added` / `removed` / `hunks` 计数；完整 diff 仍写入 `FileChange`；
    交付与审查改用计数，老记录回退 diff。
  - 验证：定向 **252 passed**（147 + 41 + 64），`git diff --check` 0。
  - 关键事实：模型上下文早已由 `agent/payload.py` 的 `DROP_DIFF_TOOLS`
    丢弃写工具 diff；本步主要减小 `tool.completed` / 检查点体积，并堵住
    `repl` 旁路。token 峰值仍需「文件状态快照 / 压调用次数」。
  - 未做：真机 benchmark（后端未启动）；`apply_patch` / `notebook_edit`
    仍带 diff（未在本步范围）。
  - 交接单：`ops/HANDOFFS/T-R2-01-claude.md`
- [ ] **T-BA 会话批量归档** — **READY_FOR_QA（质量验收整改完成）**
  - 范围：会话侧栏「管理」选择态 + 一次整批确认；`POST /threads/batch-archive`
    返回 archived/unchanged/missing；两个会话列表接口支持
    `status=all|active|archived`（先过滤后 limit）；「已归档」折叠组与单条
    「取消归档」；首页、全站左栏、任务页会话选择器只取活跃会话。
    只改 status，不停止任务、不删轮次、不加迁移。
  - 验证：原验收后端 **28 + 14 passed**、前端 **495 passed**、`check.sh --no-smoke`
    通过；本轮整改复跑后端组合 **42 passed**、前端 `src/**/*.test.ts`
    **492 passed**、tsc/eslint 0。质量验收事实核对：`DELETE /threads/{id}` 在
    代码里只把 status 改成 archived，不物理删除；前端单条归档已改为显式
    PATCH `status=archived`，物理删除只走 `DELETE /threads/{id}/permanent`。
    已归档达到取数上限 200 条时，侧栏提示「仅显示最近 200 条已归档会话」
    （产品选 A，不改接口）。
  - 交接单：`ops/HANDOFFS/T-BA-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「会话批量归档（T-BA）」
- [ ] **E12 工作区边界与整机检查/清理** — `READY_FOR_QA`
  - 范围：默认/计划/接受编辑仍拒绝工作区外路径；完全访问下跨盘只读自动放行、
    写/删逐次弹 `scope:outside_write` 审批，批准后只执行该精确动作；清理先只读
    盘点、列清单、等明确同意，系统/用户/缓存不自动删；系统盘点先查固定位置、
    单目录 60 秒、不在 C 盘根目录写扫描文件；`run_shell` 拦住 C:\Users/C:\/
    C:\Windows 的整树递归，直接返回换路提示。
  - 验证：定向 **58 passed**、前端 **486 passed**、lint/tsc 0；
    跨盘盘点出 UserTemp 84,004 文件 / 4.20 GiB；跨盘清理批准一次后 old.tmp 真删除；
    无目录检查电脑不编造；`C:\Windows\Temp` 需管理员权限；卡住任务已取消。
  - 交接单：`ops/HANDOFFS/E12-workspace-scope.md`
- [ ] **E11 所有产出物去 AI 味** — `READY_FOR_QA`
  - 范围：全局风格段新增产出物规范，覆盖文档 / 网页 / PPT / 报告；禁止套模板、
    空泛开场、流行词、编数据、满篇 bullet、emoji、AI 仪表盘与三栏图标 PPT；
    审查清单新增第 6 条；图片/图表自主判断（真实数据图/流程图/真实截图才加，
    纯装饰不加、不编造截图、不留占位）；可选「打开看看/改脚本重跑」不再判失败。
  - 验证：定向测试 **94 passed**；真实 PPT 4 页 succeeded；带数据 PPT 自主加
    原生图表 + 表格，数值与给定一致，缺失分店数据写「—」；
    docx 缺真实信息时用「待补充」占位、不编数据。
  - 交接单：`ops/HANDOFFS/E11-anti-ai-style.md`
- [ ] **E10 模型选择只显示已配置供应商** — `READY_FOR_QA`
  - 范围：新增共用 `configuredProviders`（启用 + 有 Key），应用到聊天输入框、
    全站切换器、任务创建、角色模型、头脑风暴、旧聊天首页与任务页展示；
    设置页供应商配置列表保留全部。
  - 验证：前端 **484 passed**、tsc/eslint 0；浏览器探针 3/3 断言、console 0；
    截图 `data/model-filter-only-configured.png`。
  - 交接单：`ops/HANDOFFS/E10-model-picker.md`
- [ ] **E9 日常基准 + 动作/交付修复** — `READY_FOR_QA`
  - 范围：新增 daily-001/002/003 基准（文件整理 / docx 生成 / CSV 汇总），
    `run.mjs` 支持 `--tasks-dir/--fixtures-dir` 与递归工作区快照；
    修复文件白名单被误判为整轮只读、模型只看不动手、shell 产物不计入改动、
    交付「改动 1 个文件」自相矛盾与无意义「需要你」提示；只读 shell 命令自动放行。
  - 验证：DeepSeek 日常 **3/3 PASS**（pass@1=1，平均 30.1s / 397,637 token）；
    Codex 同题 **3/3 PASS**（45/168/92s，合计 446,734 token）；
    日常整理审批卡 4→1；定向测试 **113 passed**；
    第一次全量补跑 **5094 passed / 6 failed**（只读放行与既有断言冲突，已修）；
    修复后全量 **5100 passed / 28 skipped / 0 failed**（22:23，
    `logs/pytest-full-2026-11-24b.log`）。
    演示项目「E9验收演示」真跑 data.csv **succeeded / 18s**；仓库子目录改动
    误报已修，定向 **8 passed**。
  - 交接单：`ops/HANDOFFS/E/E9-daily-benchmark.md`
- [ ] **E8 日常场景对标 Codex** — `READY_FOR_QA`
  - 范围：日常提示词收紧四条（直接动手少问、最简做法、做完核对、先给结论）；
    默认简体中文；新对话默认场景改为「日常」（存过的选择仍优先）。
  - 验证：场景/提示词 24 passed、前端 481 passed、tsc/eslint 0；
    新会话场景选择器显示「日常」，console 0。
  - 交接单：`ops/HANDOFFS/E/E8-daily-codex.md`
- [ ] **D11 结果面板精简** — `READY_FOR_QA`
  - 范围：删除改动视图的面板级标题行、范围/搜索/分组行、变更/证据/活动分段；
    改为顶部视图标签直接进入文件条 + diff。
  - 验证：前端 481 passed、tsc/eslint 0；浏览器断言无工具栏且文件条/diff 正常，
    console 0；截图 `data/e10-panel-clean.png`。
  - 交接单：`ops/HANDOFFS/D/D11-panel-slim.md`
- [ ] **D10 结果面板文件条上移** — `READY_FOR_QA`
  - 范围：窄面板下顶部四个视图标签不再竖排（按面板宽度隐藏数量/文字）；
    改动视图的文件列表移到代码区上方，横向可滚动，溢出时出现左右箭头按钮。
  - 验证：前端 481 passed、tsc/eslint 0；460px 面板下标签 62×28 单行、
    文件 chip 在 diff 上方；左右箭头滚动断言通过，console 0；
    截图 `data/e9-panel-top-files.png`、`data/e9-file-arrows.png`。
  - 交接单：`ops/HANDOFFS/D10-panel-file-strip.md`
- [ ] **D9 任务筛选状态进地址栏** — `READY_FOR_QA`
  - 范围：任务页搜索词与状态筛选写入 URL（replaceState，保留其他参数），
    刷新/收藏/从详情返回后恢复；非法状态与空白词忽略。
  - 验证：前端 481 passed、tsc/eslint 0；浏览器探针刷新后输入与选中状态都恢复，
    console 0；截图 `data/e8-task-filters.png`。
  - 交接单：`ops/HANDOFFS/D9-task-filters-url.md`
- [ ] **E7 默认模型设置入口** — `READY_FOR_QA`
  - 范围：设置页新增「默认模型」选择器 + `PUT /providers/default`；
    全站模型切换器认 `is_default`/有 Key；编辑面板首次落在默认供应商。
  - 验证：接口单测 4 passed、前端 476 passed、tsc/eslint 0；两个选择器都显示
    DeepSeek，console 0；截图 `data/e7-default-selector3.png`。
  - 交接单：`ops/HANDOFFS/E7-default-model-ui.md`
- [ ] **E6 国内模型默认与包镜像** — `READY_FOR_QA`
  - 范围：默认供应商锁定 DeepSeek 并在列表返回 `is_default`；前端默认模型名按它显示；
    子进程 pip/uv/npm 默认走境内镜像（可关、可覆盖）；办公文档默认 .docx。
  - 验证：后端相关 13 + 66 passed、前端 475 passed、tsc/eslint 0；
    `/providers` 标记正确、模型选择器显示 deepseek-flash、console 0。
  - 交接单：`ops/HANDOFFS/E6-domestic-defaults.md`
- [ ] **E5 文档产物预览** — `READY_FOR_QA`
  - 范围：结果面板打开 docx/xlsx/pptx 时抽取文本层（标题/段落/表格/工作表/幻灯片），
    以 markdown 展示并标明「样式与图表已简化」；坏文件保持二进制回执。
  - 验证：预览单测 5 passed、前端 473 passed、tsc/eslint 0；真实产物 15,277 字；
    浏览器断言通过、console 0；截图 `data/e5-office-preview.png`。
  - 交接单：`ops/HANDOFFS/E5-office-preview.md`
- [ ] **E4 文档库随包预装** — `READY_FOR_QA`
  - 范围：python-docx / openpyxl / python-pptx 纳入依赖与 uv.lock；开发 venv 与
    `desktop/build/runtime` 均已装入；构建冒烟导入与预检「应用解释器」覆盖三库。
  - 验证：三库真实生成 docx/xlsx/pptx；运行时重建 69 包 / 153.3MB；
    `uv lock --check` 通过；后端重启 health 200、前端 console 0。
  - 交接单：`ops/HANDOFFS/E4-doc-libs-bundled.md`
- [ ] **E3 长文件分段写入** — `READY_FOR_QA`
  - 范围：`write_file` 新增 `append`；BOM/换行接缝处理、二进制拒绝、空追加不变；
    工具说明与编程场景提示要求超长内容分节追加。
  - 验证：追加 + 既有写入套件 82 passed；含场景提示 48 passed；schema 检查通过；
    后端重启 health 200、前端 console 0。
  - 交接单：`ops/HANDOFFS/E3-chunked-write.md`
- [ ] **E2 开局环境预检** — `READY_FOR_QA`
  - 范围：开局探测 python / `py -3`（含 pip）/ `.venv` / node / git / uv，
    注入系统提示并写 `environment.profiled`；5 分钟缓存、失败不阻塞任务。
  - 验证：预检单测 5 passed；含 runtime 路径的相关套件 53 passed；本机实测 833ms；
    后端重启 health 200、前端 console 0。
  - 交接单：`ops/HANDOFFS/E2-env-preflight.md`
- [ ] **D8 界面状态记忆** — `READY_FOR_QA`
  - 范围：侧栏收窄 / 结果面板开合刷新后保持；同一次打开内切换会话回到离开时的
    滚动位置（原本贴底则继续贴底）；结果面板状态按会话隔离（不再残留上一条会话
    的文件标签）。
  - 验证：前端 469 passed、tsc/eslint 0；浏览器探针侧栏、面板、滚动、面板标签
    四组断言通过，console 0。
  - 交接单：`ops/HANDOFFS/D8-interface-memory.md`
- [ ] **D7 结果面板重设计 + 无产出隐藏 + 横向滚动** — `READY_FOR_QA`
  - 范围：无产出的普通对话不再显示右侧面板与入口；面板重设计（620px、四视图
    pill、文件标签行、精简工具栏）；diff/文本预览支持横向滚动，不再强制换行。
  - 验证：前端 469 passed、tsc/eslint 0、journey 通过；浏览器断言面板隐藏/显示
    与 `scrollWidth 961 > clientWidth 444` 均通过，console 0。
  - 交接单：`ops/HANDOFFS/D7-result-panel.md`
  - 截图：`data/panel-no-output.png`、`data/panel-redesign-thread-clean.png`
- [ ] **D6 全页面玻璃一致性 + 丝滑 v1** — `READY_FOR_QA`
  - 范围：12 页浏览器审计 console 0；任务页补玻璃卡片；会话「回到底部」、
    加载骨架屏、页面入场动画；469 前端测试、tsc/eslint 0、journey 通过。
  - 交接单：`ops/HANDOFFS/D6-page-smoothness.md`
  - 证据：`data/glass-audit/`、`data/liquid-jump-bottom2.png`
- [ ] **D5 液态玻璃皮肤 v1** — `READY_FOR_QA`（视觉草案，等你看效果）
  - 范围：**只保留一套浅色液态玻璃**（深色/切换已移除）；极光背景、镜面高光、
    主框架/面板/输入区玻璃化；469 前端测试、tsc/eslint 0、journey 通过，
    首页/设置/会话 console 0。
  - 交接单：`ops/HANDOFFS/D5-liquid-glass-v1.md`
  - 截图：`data/liquid-glass-light-v7.png`、`data/liquid-glass-light-thread2.png`、
    `data/liquid-glass-light-settings3.png`、`data/liquid-glass-dir-picker2.png`
  - 修复：侧栏改 `glass-side`（更实、无噪点）；移除覆盖 portal 的 `body > *` 规则，目录选择框恢复。
  - 待下一轮：全部页面走查、弹窗/菜单细化、丝滑优化。
- [ ] **K2 会话授权撤销（T-A2）** — `READY_FOR_QA`
  - 范围：列出/单类撤销/全部撤销本会话授权；会话条入口；
    后端 4 passed、前端 469 passed、tsc/eslint 0、journey 通过、OpenAPI 135 paths。
  - 交接单：`ops/HANDOFFS/K2-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「会话授权撤销（K2）」。
- [ ] **K1 自学习可见闭环** — `READY_FOR_QA`
  - 范围：经验/偏好快照、逐条退役、候选偏好采用、体检告警；设置页「记忆 → 自学习」；
    后端 5 passed、前端 467 passed、tsc/eslint 0、journey 通过、OpenAPI 含 4 端点。
  - 交接单：`ops/HANDOFFS/K1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「自学习可见闭环（K1）」。
- [ ] **U1 结果状态条** — `READY_FOR_QA`
  - 范围：过程摘要行增加完成/失败/等你批准等状态词；失败段标红；未知终态显示「已结束」；
    前端 464 passed、tsc/eslint 0、journey 通过。
  - 交接单：`ops/HANDOFFS/U1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「结果状态条（U1）」。
- [ ] **B3c 多文件应用预览** — `READY_FOR_QA`
  - 范围：按需启动 `node <文件>` 预览服务、回环端口、Job Object、10 分钟 TTL、
    进程树回收；前端预览 Tab；后端 22 passed、前端 461 passed、tsc/eslint 0、journey 通过。
  - 交接单：`ops/HANDOFFS/B3c-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「多文件应用预览（B3c）」。
- [ ] **B3b 单文件内嵌预览** — `READY_FOR_QA`
  - 范围：HTML sandbox 静态预览、Markdown 渲染、JSON 格式化、CSV/TSV 表格、截断提示；
    前端 458 passed、tsc/eslint 0、journey 通过。
  - 交接单：`ops/HANDOFFS/B3b-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「单文件内嵌预览（B3b）」。
- [ ] **B3a 产物面板增强** — `READY_FOR_QA`
  - 范围：产物搜索、按类型分组、复制路径、导出 JSON/Markdown；
    前端 452 passed、tsc/eslint 0、journey 通过。
  - 交接单：`ops/HANDOFFS/B3a-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「产物面板增强（B3a）」。
- [ ] **Y1 成本闸设置入口** — `READY_FOR_QA`
  - 范围：`/settings/agent` 返回并接受 `budget`；设置页新增金额上限与价目表编辑；
    价目表本地校验；后端 30 passed、前端 447 passed、tsc/eslint/journey 通过。
  - 交接单：`ops/HANDOFFS/Y1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「成本闸设置入口（Y1）」。
- [ ] **X1 前端跳转文案登记补齐** — `READY_FOR_QA`
  - 范围：补登记 `list_only` / `ui_repair_reserve`；前端单测 443/443、tsc 0、journey 通过。
  - 交接单：`ops/HANDOFFS/X1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾 B10 节「复跑补记（X1）」。
- [ ] **T-A1 会话级审批授权** — `READY_FOR_QA`
  - 范围：完全访问下同类动作批准一次后本会话不再逐条问；驳回不再反复问。
  - 交接单：`ops/HANDOFFS/T-A1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「会话级审批授权」一节。
- [ ] **B1 任务工作台状态真相** — `READY_FOR_QA`
  - 范围：任务成功但计划未回写时显示「计划未更新」；没有验收条件时不再显示 `0/0 验收`。
  - 交接单：`ops/HANDOFFS/B1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「任务工作台状态真相」一节。
- [ ] **C1 交付未完成不再判成功** — `READY_FOR_QA`
  - 范围：交付自述「未达门槛」「需要你补足」时判 failed，不再 succeeded。
  - 交接单：`ops/HANDOFFS/C1-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「交付未完成不再判成功」一节。
- [ ] **C2 可量化验收 + 自动补齐** — `READY_FOR_QA`
  - 范围：`file_min_chars` 判据；未过时回执行节点，提示用 `wc -m` 实测补齐。
  - 交接单：`ops/HANDOFFS/C2-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「可量化验收 + 自动补齐」一节。
- [ ] **C3 验收结论重试与未判定** — `READY_FOR_QA`
  - 范围：验收 JSON 解析失败重试一次；两次失败记「未判定」而不是「没覆盖」。
  - 交接单：`ops/HANDOFFS/C3-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「验收结论重试与未判定」一节。
- [ ] **C4 审批过期治理** — `READY_FOR_QA`
  - 范围：审批卡默认有效期 30 分钟 → 24 小时，环境变量仍可调短。
  - 交接单：`ops/HANDOFFS/C4-claude.md`
  - 验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「审批过期治理」一节。
- [ ] **C5 上下文账本与旧工具参数折叠** — `READY_FOR_QA`
  - 范围：tool_calls 计入水位；旧写类调用的大参数折成一句说明。
  - 交接单：`ops/HANDOFFS/C5-claude.md`
  - 验收记录：末尾「上下文账本与旧工具参数折叠」一节。
- [ ] **C6 真实大任务复跑 + 自我修复** — `READY_FOR_QA`
  - shopbench 复跑任务 `7ce98397`：27 步 succeeded，11 文件，18 项测试通过；
    但交付自述前端导航坏（C1 新词表已覆盖该表述）。
  - 修复任务 `37032c7b`：32 步 succeeded，改 `public/app.js` 一处。
  - 独立浏览器复核：`http://127.0.0.1:4173` 六视图 16/16 断言通过、0 控制台错误；
    `file://` 因 API CORS 不适用，正确启动方式是 `node server.js`。
  - 交接单：`ops/HANDOFFS/C6-claude.md`
  - 验收记录：末尾「真实大任务复跑与自我修复」一节。
- [ ] **D1 生成应用去 AI 味与安全加固** — `READY_FOR_QA`
  - 范围：shopbench 生成应用视觉重做（工业风三栏 AI 工作台）+ 安全头/路径/输入加固；
    `CODING_INSTRUCTIONS` 增加反 AI 仪表盘与安全底线两条。
  - 交接单：`ops/HANDOFFS/D1-claude.md`
  - 验收记录：末尾「生成应用去 AI 味与安全加固」一节。
- [ ] **D2 harness 生成质量闸门** — `READY_FOR_QA`
  - 范围：前端改动必须有最新代码上的 browser_check（断言 + 动作 + 零错误）；
    审查清单增加反 AI/安全/规范；修复并行图片消息顺序与 file:// 越界；
    交付文本显示浏览器自检。
  - 交接单：`ops/HANDOFFS/D2-claude.md`
  - 验收记录：末尾「harness 生成质量闸门」一节。
- [ ] **D3 主仓库代码规范与安全审计** — `READY_FOR_QA`
  - 范围：ESLint 清零、Markdown 链接白名单、API 安全头、上传/原始文件降级、
    provider URL 校验、token 定长比较、422 脱敏；桌面外链/打开路径/静态边界；
    `host_bypass` 会话档安全收紧；工具预算与 bash/hook 产品修复。
  - 交接单：`ops/HANDOFFS/D3-claude.md`
  - 验收记录：末尾「主仓库代码规范与安全审计」一节。
- [ ] **D4 依赖漏洞、严格 CSP 与弃用迁移** — `READY_FOR_QA`
  - 范围：Next 16.4.6 / pypdf 6.21.4 / certifi / opentelemetry 升级，前后端审计归零；
    构建期脚本哈希严格 CSP（out + `.next/server/app|pages`）；
    `datetime.utcnow()` 迁移。
  - 交接单：`ops/HANDOFFS/D4-claude.md`
  - 验收记录：末尾「依赖漏洞、严格 CSP 与弃用迁移」一节。
- [ ] **B2 新对话首屏（两场景版）** — `READY_FOR_QA`
  - 范围：首屏极简（大图标 + 大标题）；新对话默认无目录；无目录 = 纯聊天
    （禁用文件/命令工具），选目录后恢复代码 Agent；有目录时标题为
    「我们要在 <目录> 做什么？」（无括号）；输入框位置不变（目录/场景在底部工具条）；
    浏览器可用的目录选择/更换（后端列目录，不再手填路径）；编程主 / 日常辅；
    编程提示词对照 Claude Code，日常对照 Codex；文档/头脑风暴/逆向不开放新建。
  - 交接单：`ops/HANDOFFS/B2-claude.md`
  - 验收记录：末尾「新对话首屏（B2，两场景版）」一节。
- [ ] **B4 场景策略层与否定式指令** — `READY_FOR_QA`
  - 范围：`no_tools` / `read_only` 硬边界（只回复、不要读取、不要修改/执行）；
    只读指令按只读执行跑（不是给判断），模型判 act/advise 都会归一到只读执行；
    场景 `ScenarioPolicy`（规划/验证/预算比例）真实生效；
    `task.started.scenario_policy` 可追溯；清单输出强制 Markdown 列表；
    `list_only` 只挂 list_files/glob，收尾直接用工具回执拼列表。
  - 交接单：`ops/HANDOFFS/B4-claude.md`
  - 验收记录：末尾「场景策略与否定式指令（B4）」一节。
- [ ] **B5 四任务三家对标与内核修正** — `READY_FOR_QA`
  - 范围：同一组 4 个任务（编程新增/修 bug、数据 CSV、文档 README）对
    空白格 / Claude Code / Codex 机器判分；隐藏验收、环境隔离、假分数修正；
    修复“不要改变 API 名称被锁只读”与“无目录动作任务被当纯聊天”两个缺陷。
  - 结果：ours 96.5 / Claude 97.5，双方 4/4 通过；ours 平均 225K token，
    Claude 128K；当时旧版 Codex 模型/额度不可用，未对比。
  - 交接单：`ops/HANDOFFS/B5-claude.md`
  - 报告：`docs/capability-benchmark-2026-11-22.md`
  - 验收记录：末尾「四任务对标（B5，2026-11-22）」一节。
- [ ] **B6 T25 文档任务成本收敛** — `READY_FOR_QA`
  - 范围：链式命令解释器替换、文档-only 工具收窄（34→16）、文档-only
    执行纪律；目标不改 UI、不牺牲隐藏验收。
  - 结果：T25 从 399K/16 轮/43s 降到 **136–156K/9–10 轮/16–23s**，
    隐藏验收仍 100；四任务最终复跑 ours 98.7 / Claude 94.4（双方 4/4），
    T25 ours 135K vs Claude 178K；Codex 运行时为旧版不可用，之后 v0.171+spark
    TUI 可用，但 `codex exec` 自动化仍超时/apply_patch 语法错误。
  - 交接单：`ops/HANDOFFS/B6-claude.md`
  - 报告：`docs/t25-cost-2026-11-22.md`
  - 验收记录：末尾「T25 成本收敛（B6，2026-11-22）」一节。
- [ ] **B7 真实多文件任务复跑** — `READY_FOR_QA`
  - 范围：用原 shopbench LR10D 商品资料任务（附件 PDF）在干净工作区复跑，
    记录状态、步数、审批/介入、token、文件、测试、浏览器与失败点；
    不把小任务结论外推成大任务能力。
  - 结果：第一轮因“不要修改工作区之外的文件”被误判只读、0 改动却 succeeded；
    已修 `directives.py` 并补回归。第二轮任务 failed（26 步 / 29 调用 /
    1.74M token / 12 文件 / 9 测试全过）；失败原因是前端改动后没调用
    `browser_check`，且 1.5M 预算在 UI 修复前耗尽。独立浏览器 13/13 动作通过。
    **B10 后续复跑 succeeded**：28 步 / 32 调用 / 1.757M token / 16 变更 /
    `npm test` 17 passed / browser_check 2 次 / 验收 3/3。
  - 交接单：`ops/HANDOFFS/B7-claude.md`
  - 报告：`docs/b7-shopbench-2026-11-22.md`
  - 验收记录：末尾「真实多文件任务复跑（B7，2026-11-22）」一节。
- [ ] **B8 前端交付闸门自修** — `READY_FOR_QA`
  - 范围：预算耗尽时保留一次 UI 修复；该回合允许 browser_check/run_shell；
    browser_check 的 expect/steps 提示；生成页面自带 favicon。
  - 结果：B7 复跑里保留修复生效并调用 6 次 browser_check，但模型断言写错；
    同一工作区的 UI 自检小任务 **succeeded**（19 步 / 844K / 9 expect + 6
    动作，控制台 0 错误）——但该任务预算未耗尽、任务书已明确检查顺序。
    最终提示词下从头复跑完整 B7 **仍 failed**（28 步 / 33 调用 / 1.67M；
    `reserved_ui_repair=true` 但 browser_check 调用 0 次）。下一步需确定性
    强制/自动探测（B9），不能继续只加提示。
  - 交接单：`ops/HANDOFFS/B8-claude.md`
  - 报告：`docs/b8-ui-gate-2026-11-22.md`
  - 验收记录：末尾「前端交付闸门自修（B8，2026-11-22）」一节。
- [ ] **B9 确定性 UI 冒烟** — `READY_FOR_QA`
  - 范围：模型完全不调用 browser_check 时，验证节点自己启动工作区 node 服务，
    用 browser_probe 做“打开 + reload + 正文非空 + 控制台零错误”的兜底；
    不替代模型交互验收，只保证页面可打开。
  - 结果：真机 B7 工作区自动冒烟 **passed**（1 assert / 2 steps / 0 console）；
    B9 相关回归 **60 passed**。完整 B7 从头复跑仍 failed：模型这次调用了
    browser_check，但 click/type 后没有 wait，两条断言在 AI 响应前执行失败；
    自动冒烟按设计只覆盖“完全没跑”，不覆盖“跑了但写错”。
  - 交接单：`ops/HANDOFFS/B9-claude.md`
  - 报告：`docs/b9-ui-smoke-2026-11-22.md`
  - 验收记录：末尾「确定性 UI 冒烟（B9，2026-11-22）」一节。
- [ ] **B10 browser_check 动作可靠化** — `READY_FOR_QA`
  - 范围：click/type 后自动 settle；长页面点击前 scrollIntoView；
    steps 的 `assert` 支持 `{selector,min}`；避免“检查脚本本身误报”。
  - 结果：模型同款无 wait 步骤 **5 expect/7 动作全过**；完整 B7 从头复跑
    **succeeded**：28 步 / 32 调用 / 1,757,693 token / 16 变更 / `npm test`
    17 passed / browser_check 2 次 / 验收 3/3。成本仍高。
  - 交接单：`ops/HANDOFFS/B10-claude.md`
  - 报告：`docs/b10-browser-check-2026-11-23.md`
  - 验收记录：末尾「browser_check 动作可靠化（B10，2026-11-23）」一节。

## 待办池（未排期）
- [x] **browser_probe Windows 进程树回收** — **已修**
  - 现象：`browser_probe.mjs` 的 `child.kill()` 只杀 Chrome 主进程，
    GPU/renderer/crashpad 残留；一次失败的全量可留下 240 个 headless 进程、
    约 7.7GB 内存，导致后续全量被系统回收。
  - 做法：Windows 上按本轮唯一的 `--user-data-dir` 过滤并清理所有 Chrome
    进程；实测 browser_check 单跑后残留 **0**。
- [x] **ARCH-06b：SQLite `foreign_keys=ON` 前审计** — **已实现，全量通过（仅 browser 环境红）**
  - 证据：开启外键后首轮全量 **63 failed**，其中 45 项是测试只造
    `agent_events` / `tool_calls` / `agent_runs` 子行、不造父任务；14 项空
    `session_factory` 告警已单独修。
  - 做法：给事件/工具调用/运行台账的测试夹具补父任务/加 flush；再打开
    `PRAGMA foreign_keys=ON`。清理残留 Chrome 后全量
    **5163 passed / 3 failed / 24 skipped**。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-ARCH-06b-claude.md`
- [x] **MASTER-LAUNCH-PLAN 批次 1B：BUG-01 / BUG-05** — **已实现（READY_FOR_QA）**
  - BUG-01：`fs_broker.py` 新增 `read_bytes`，隔离模式下读图不再退化成文本。
  - BUG-05：`sandbox/runner.py` 改 `CREATE_SUSPENDED` → 挂 Job → `ResumeThread`。
  - 交接单：`ops/HANDOFFS/T-LAUNCH-1B-claude.md`
- [ ] **NEXT-DEV-v3：压调用次数第二轮 + P1 修复** — **已转入 NEXT-DEV-v5**。主体（工具说明书瘦身、执行层并发、提示词、P1 修复、质量底线、速度）已在 v4/v5 承接；遗留「快照内容摘要与变更计数」列入 v5 P2-补。


- [ ] **B3：右侧结果区与产物归集继续重做** — **已转入 NEXT-DEV-v5（P2-补 第 9 项）**。
- [ ] **T-A2：会话授权撤销 UI / HTTP 端点** — **已转入 NEXT-DEV-v5（P2-补 第 10 项）**。存储层已有 `revoke` / `purge_thread`，缺 UI 与 HTTP 端点。
