# 项目状态

- 更新：2026-11-27
- 当前任务：**ARCH-01d（请求装配外移）**
- 状态：**READY_FOR_QA（全量待 ARCH-01 收口）**
- 清单文档：docs/MASTER-LAUNCH-PLAN.md
- 说明：Step 1（1A/1B/ARCH-06b）、ARCH-05、ARCH-01a/01b/01c 已完成；
  ARCH-01d 把 `_run_request` 等请求装配函数移到
  `services/orchestration/runner.py`，launch/recover 仍在 tasks.py。
- 开发交接单：`ops/HANDOFFS/T-LAUNCH-ARCH-01d-claude.md`、`ops/HANDOFFS/T-LAUNCH-ARCH-01c-claude.md`、`ops/HANDOFFS/T-LAUNCH-ARCH-01b-claude.md`、`ops/HANDOFFS/T-LAUNCH-ARCH-01a-claude.md`、`ops/HANDOFFS/T-LAUNCH-ARCH-05-claude.md`、`ops/HANDOFFS/T-LAUNCH-ARCH-06b-claude.md`、`ops/HANDOFFS/T-LAUNCH-1B-claude.md`、`ops/HANDOFFS/T-LAUNCH-1A-claude.md`、`ops/HANDOFFS/T-R2-07-claude.md`、`ops/HANDOFFS/T-R2-06-claude.md`、`ops/HANDOFFS/T-R2-05-claude.md`、`ops/HANDOFFS/T-R2-04-claude.md`、`ops/HANDOFFS/T-R2-03-claude.md`、`ops/HANDOFFS/T-R2-02-claude.md`、`ops/HANDOFFS/T-R2-01-claude.md`、`ops/HANDOFFS/T-BA-claude.md`、`ops/HANDOFFS/E12-workspace-scope.md`、`ops/HANDOFFS/E11-anti-ai-style.md`、`ops/HANDOFFS/E10-model-picker.md`、`ops/HANDOFFS/E/E9-daily-benchmark.md`、`ops/HANDOFFS/E/E8-daily-codex.md`、`ops/HANDOFFS/D/D11-panel-slim.md`
- 报告：`data/batch-archive-managing-1440.png`、`data/batch-archive-archived-1440.png`、`data/model-filter-only-configured.png`、`data/e11-daily-default.png`、`data/e10-panel-clean.png`
- E 盘绑定：基准工作区 `.bench-tmp/`、pytest TEMP `.tmp-test/`、后端 TEMP `.tmp/`、
  桌面壳/dev 子进程 TEMP `E:\env\blank-tmp`、UV 缓存 `E:\env\uv-cache`；
  均不在 C 盘。已清理 C 盘本项目临时目录 **1,347 个 / 约 1.33GB**；
  桌面壳单测 **104 passed**。
- 同批待验收：T-LAUNCH-1B、T-LAUNCH-1A、T-R2-06、T-R2-05、T-R2-04、T-R2-03（进行中）、T-R2-02、T-R2-01、T-BA、E12、E11、E10、E9、D11、D10、D9、E7、E6、E5、E4、E3、E2、E1、D8、D7、D6、D5、K2、K1、U1、B3c、B3b、B3a、Y1、X1、B10、B2、B4、B5、B6、B7、B8、B9、T-A1、B1、C1–C6、D1–D4（见 `TODO.md` 与
  `docs/validation/acceptance-2026-11-21.md`）
- 验收记录：`docs/validation/acceptance-2026-11-21.md`（末尾「日常场景对标 Codex（E8）」「结果面板精简（D11）」）
- 最近验证：
  - **2026-11-27 ARCH-01d 请求装配外移（Claude）**：`_factory_from`、
    `_resolve_provider`、`dispatch_of`、`_run_request`、`_routable` 移到
    `services/orchestration/runner.py`；tasks.py 保留同名导入。
    worker/agent/threads/projects **65 passed**；审批恢复/计划模式/续跑/
    交付快照 **88 passed**；全量待 ARCH-01 收口。
    交接单 `ops/HANDOFFS/T-LAUNCH-ARCH-01d-claude.md`。
  - **2026-11-27 ARCH-01c 执行函数外移（Claude）**：`_execute_claimed_run` 与
    `LEASE_HEARTBEAT_SECONDS` 移到 `services/orchestration/runner.py`；
    `test_run_lease` 的 monkeypatch 目标同步改为 runner。导入检查通过；
    租约/worker/通知/触发器 **61 passed**；全量（排除 3 项 browser 环境红）
    **5164 passed / 24 skipped / 3 deselected**（15:00，
    `logs/pytest-arch01c-2026-11-27.log`）。交接单
    `ops/HANDOFFS/T-LAUNCH-ARCH-01c-claude.md`。
  - **2026-11-27 ARCH-01b 失败与通知外移（Claude）**：把
    `_record_run_failure`、`_graph_decision`、`_resume_decision_delivered`、
    `_notify_kind`、`_origin_from_trigger`、`_record_notify_decision` 移到
    `services/orchestration/runner.py`；`_execute_claimed_run` 暂留 tasks.py。
    通知/租约/worker/触发器 **61 passed**；全量（排除 3 项 browser 环境红）
    **5164 passed / 24 skipped / 3 deselected**（21:06，
    `logs/pytest-arch01b-2026-11-27.log`）。交接单
    `ops/HANDOFFS/T-LAUNCH-ARCH-01b-claude.md`。
  - **2026-11-27 ARCH-01a 编排层外移（Claude）**：新增
    `services/orchestration/runner.py`，把 `persist_task` / `_record_dispatch`
    从 `api/routes/tasks.py` 移出；`tasks.py` 保留同名导入，worker/IM/测试的
    调用路径不变。建任务/线程/worker 45 passed、创建/派发/触发/IM/计划模式
    102 passed；全量（排除 3 项 browser 环境红）
    **5164 passed / 24 skipped / 3 deselected**（18:18，
    `logs/pytest-arch01a-2026-11-27.log`）。交接单
    `ops/HANDOFFS/T-LAUNCH-ARCH-01a-claude.md`。
  - **2026-11-27 ARCH-05 worker 有界并发（Claude）**：`worker.run_once` 改
    `asyncio.Semaphore(BLANK_WORKER_CONCURRENCY)`，默认 2；抽出
    `_run_candidate`，单条候选异常不带走同轮其他 run。worker/调度 8 文件
    **75 passed**；全量（排除 3 项 browser 环境红）
    **5164 passed / 24 skipped / 3 deselected**（20:12，
    `logs/pytest-worker-2026-11-27.log`）。附带修 browser_probe 的 Windows
    进程残留，browser_check 单跑后残留 **0**。交接单
    `ops/HANDOFFS/T-LAUNCH-ARCH-05-claude.md`。
  - **2026-11-27 ARCH-06b 外键收口（Claude）**：SQLite 连接恢复
    `PRAGMA foreign_keys=ON`；修 18 个测试文件的父子插入顺序/父行缺失，
    孤儿行用例改用独立 `sqlite3` 连接临时关外键造数。已知外键子集
    **212 passed**、产物生命周期 **19 passed**、TypeError 组 **8 passed**；
    首次全量因 browser_check 残留 240 个 headless Chrome 导致内存不足被停止，
    精确清理测试进程后全量 **5163 passed / 3 failed / 24 skipped**
    （23:54，`logs/pytest-fk-b-2026-11-27.log`），3 项为 browser_check
    提权临时目录环境红。交接单 `ops/HANDOFFS/T-LAUNCH-ARCH-06b-claude.md`。
  - **2026-11-27 MASTER-LAUNCH-PLAN 批次 1B（Claude）**：新增
    `workspace.read_binary_file` 与 broker `read_bytes`，`read_file` 在
    broker/隔离下读图不再退化成文本；`LocalSandbox` 改
    `CREATE_SUSPENDED` → 挂 Job → `windows_job.resume_process` 枚举线程恢复，
    消掉先起进程再挂 Job 的竞态。定向 **27 + 170 + 25 passed**；全量
    **5163 passed / 3 failed / 24 skipped**（17:13，
    `logs/pytest-1b-2026-11-27.log`）；3 项仍为 browser_check 提权临时目录
    环境红。交接单 `ops/HANDOFFS/T-LAUNCH-1B-claude.md`。
  - **2026-11-26 MASTER-LAUNCH-PLAN 批次 1A（Claude）**：BUG-02 `FallbackAdapter`
    输出预算透传、BUG-03 只读场景补禁 `write_file`/`edit_file`/`notebook_edit`、
    BUG-04 AppContainer 属性号 `0x20009`、ARCH-06 部分（SQLite WAL /
    `synchronous=NORMAL` / `busy_timeout`）；连带修 `create_task` 会话/任务插入顺序与
    `runtime._prepare` 空会话工厂告警。首轮开启 `foreign_keys=ON` 全量 **63 failed**
    （45 项测试夹具只造子行、14 项空工厂告警）；回退外键开关、修空工厂保护后，
    定向 13 文件 **349 passed**，全量 **5162 passed / 3 failed / 24 skipped**
    （15:56，`logs/pytest-1a-b-2026-11-26.log`）；3 项为提权 pytest 临时目录 ACL
    导致 browser_check 读不到页面（对照探针打开仓库 HTML 与 `E:\env\blank-tmp`
    文件均 `ok`）。交接单 `ops/HANDOFFS/T-LAUNCH-1A-claude.md`。
  - **2026-11-26 NEXT-DEV-v5 P0/P1/P2**：P0 推测执行 `speculative` 缺属性兼容，
    6 个 stream 回归全绿；P1 lsp 根因定位为「TEMP 在仓库内 → git check-ignore
    把整个临时工作区判 ignored」——产品修 `_git_ignored` 根探测，测试加
    `GIT_CEILING_DIRECTORIES`，lsp **40 passed**、同根因 worktree **128 passed**；
    P1 提示词压轮次，真机 T09 **100 分 / 12 轮 / 283K / 46s**；T01 **100 分 /
    8 轮 / 155K**；P2 202 断言、worktree、output_styles、快照摘要/变更计数收口，
    `evals/compare.mjs` 校验固定 UTF-8（修复一次 T09 假红 55→100）。
    全量最终 HEAD **5154 passed / 28 skipped / 0 failed**（20m28s）。
    交接单 ops/HANDOFFS/T-R2-08-claude.md。
  - **2026-11-25 T-R2-06/07 质量验收（Codex）**：v4 任务 1b/1c/方案B/任务3/4/任务5(部分)三关卡实测。T-R2-07 定向 **122 passed / 3 failed**；真机 T09 **100 分**（token 611K→**360K，降约 41%，达成 ≥30%**、**15 轮，调用 ≤12 未达**，方案 B 生效 minimal 恢复）；全量 **5117 passed / 35 failed**。**关键发现：任务 4 推测执行引入 P1 回归**（_clear_speculation 访问 session.speculative 无兼容，6 个 stream 测试 AttributeError）；**lsp 13 失败真实复现**（任务 2 未真正修复，0 失败结案不成立）；任务 5 部分未完成。任务 3（done_when/钩子/范围）实现正确。**退回开发**。验收单 ops/HANDOFFS/T-R2-06-07-codex.md。
  - **2026-11-25 NEXT-DEV-v4 任务 3/4 + 测试清理第二批（代码完成，未跑测试）**：
    任务 3 三项落地——钩子 `on_failure=block`（阻断型事件失败转拒绝）、
    `done_when` 逐条对账（只有 `met` + 非空证据才放行）、审查确定性范围
    （git base/head/merge-base + 四组路径，baseSha 开局取一次）。任务 4 推测性
    只读执行：流式参数完整即提前跑 `read_file/glob/search_code`，仅 bypass、
    结果只缓存，执行点仍走网关权限与记账，`file_state` 读记录延迟到真交付。
    任务 5 第二批 6 个审批 202 测试已改（含 `unknown_kinds_e2e_v148` 与
    `stop_failure_hooks_v55`）。按用户指示本轮不跑测试，只做 AST 与
    `git diff --check`；见 `ops/HANDOFFS/T-R2-07-claude.md`。
  - **2026-11-25 NEXT-DEV-v4 1b/1c + 测试清理第一批**：1b 新增
    `parallel_write_tools`，不同文件 + bypass 档并发写/改，同文件/需审批仍串行；
    1c 提示词明确多文件同批、写后不重读；T09 `expect_files` 方案 B 改为
    `tools/`；清理测试 worktree 残留；审批 202 断言更新 5 个文件。定向
    **110 + 123 + 93 passed**；lsp 文件单独 **40 passed**，与审批测试组合
    **123 passed**，全量 13 失败未复现。真机 T09 与全量失败清单待跑，见
    `ops/HANDOFFS/T-R2-06-claude.md`。
  - **2026-11-25 T-R2-05 质量验收（Codex）**：v4 任务 1a 工具说明书瘦身三关卡实测。定向 **219 passed**；真机 T09 **94 分**（token 611K→**372K，降约 39%，达成 ≥30% 目标**、15 轮，但 minimal=0.6 扣 6 分——模型多建 tools/check_links.py 可执行入口，评分 expect_files 未含，非功能错误）；真机 T01 **100 分**（工具调用正确性未破坏）。代码审查无 P0/P1。**token 目标达成，但 T09 分数 100→94 需决策处理**。验收单 ops/HANDOFFS/T-R2-05-codex.md。
  - **2026-11-25 NEXT-DEV-v4 任务 1a：工具说明书瘦身**：常驻工具 **34→30**
    （Skill 市场四工具按需，`tool_search` 可拉回）；压缩 `run_shell` /
    `request_user_input` / `search_code` / `edit_file` / `update_plan` /
    `set_goal` / `task_update` / `propose_skills` / `report_findings` 文案。
    常驻说明 **22,694→19,457 字符（-14.3%）**，T09 文档任务 **11,291→10,354
    （-8.3%）**；`SCHEMA_BUDGET_CHARS` 22,700→**20,000**。定向 **219 passed**。
    真机 T09/T01 对照未跑（需重启后端），见 `ops/HANDOFFS/T-R2-05-claude.md`。
  - **2026-11-25 T-R2-04 质量验收（Codex）**：三关卡实测通过。定向 **96 passed**；真机 T09 对照 **100 分**（succeeded、minimal=1，此前 85 因全盘扫描系统缓存扣 15，P1 修复生效）；修复电脑端到端实测走授权盘点通道（先只读盘点、列清单、单目录时间上限、脚本放工作区 scratchpad、无整盘递归）。代码审查无 P0/P1。**token 611K/22 轮，任务 1（压 token）未做，仍待开发。** 验收单 ops/HANDOFFS/T-R2-04-codex.md。
  - **2026-11-24 NEXT-DEV-v3 任务 2：P1 修复**：普通任务提示只查工作区、整机
    检查需明确请求；`slow_scan_hint` 覆盖 `%SystemDrive%` / `%SystemRoot%` /
    `%windir%` / `$env:SystemDrive`；`SnapshotStore` 在扫描/复制/`changed_since`
    三处排除系统盘根变量与 Windows Caches，`evals/compare.mjs` 同步忽略。
    定向 **96 passed**、`node --check evals/compare.mjs` 0。真机 T09 对照未跑
    （需重启后端加载），见 `ops/HANDOFFS/T-R2-04-claude.md`。
  - **2026-11-24 T-R2-02/03 质量验收（Codex）**：T-R2-02 快照 + T-R2-03 提示词压次数验收。定向 **152+77+78 passed**；真机 T09 ×2：调用 19→14/15（降 21-26%，目标 ≤12 未达）、token 487K→350-407K（降 17-28%，目标 ≥30% 未达）、任务 succeeded/测试通过/自证。**快照真实生效**：9 次 micro:below、reads_folded 累计 8 次、chars_saved 8,708。新发现 P1（非本次回归）：模型在 T09 跑 dir /s /b %SystemDrive% 全盘扫描，系统缓存文件被算进 changed 导致 minimal=0，需单独处理。验收单 ops/HANDOFFS/T-R2-02-03-codex.md。
  - **2026-11-24 NEXT-DEV-v2 任务二第一刀：压调用次数（提示词）**：系统提示
    允许「不同文件的独立写/改」在自动放行档同一批发出，并明确「刚写过的文件
    编辑时直接用写入原文做 old_string，不要为找锚点重读」。依据 T09 诊断的
    3 次重读 + 3 次 write 分三轮 + 3 次 edit 分三轮，目标 ≤12 次。定向
    **338 passed**；真机 T09 对照未跑（需确认后端已加载本次改动），见
    `ops/HANDOFFS/T-R2-03-claude.md`。
  - **2026-11-24 NEXT-DEV-v2 任务一：文件状态快照**：`FileStateCache` 记录
    行数/最近读写，`render_snapshot()` 生成路径+行数+短指纹状态块；旧
    `read_file` 结果在压缩时换成快照指针；token 模式低于 0.82 水位也会做一次
    确定性瘦身（`micro:below`），不再等 1M 窗口永远到不了的水位。定向
    **338 passed**（27 个文件，含 T-R2-01 编辑/交付/审查/回执与 Agent 集成），
    `git diff --check` 0。真机 T09 对照未跑（未确认后端已加载本次改动），见
    `ops/HANDOFFS/T-R2-02-claude.md`；调用分布见 `dev/T09-CALL-DIAGNOSIS.md`。
  - **2026-11-24 T-R2-01 人工验收（Codex 代做，端到端）**：后端 SQLite 启动，创建真实任务（write_file 新建 + edit_file 修改 receipt_check.txt）succeeded（8 调用/160K/6 步/2 变更）。前端任务详情页交付四行正确（改动 1 个文件/1 处 Diff）、事件流 5 条 tool.completed 仅 summary 无 diff、检查点 artifact 仅计数无 diff（write 316B / edit 410B）、/diff 接口完整 diff、文件内容与验收标准一致；浏览器探针 console 0 / 4xx 0 / 断言全过。验收单 `ops/HANDOFFS/T-R2-01-codex.md`。
  - **2026-11-24 T-R2-01 质量验收（Codex）**：三关卡验收通过，待人工验收。定向 **252 passed**（147+41+64）；直接实测确认 edit_file/write_file 回执无 diff、added/removed/hunks 计数正确、append 追加正确、老记录回退正确；FileChange 仍存完整 diff（前端只从 FileChange 读 diff，事件结构变化兼容）；payload.py:70/237 确认 DROP_DIFF_TOOLS 早已丢弃模型上下文 diff——本步主要收益是减小 tool.completed 事件与检查点体积、堵住 repl 旁路。P2：write_file append 为范围外新增（交接单未声明）；acceptance 文档 1 处行尾空行。验收单 ops/HANDOFFS/T-R2-01-codex.md。
  - **2026-11-24 GOAL-ROADMAP-v2 第 1 步：写类工具只回执**：`edit_file` /
    `write_file` 的 outcome 去掉完整 diff，改带 `added` / `removed` / `hunks`
    计数；完整 diff 仍进 `FileChange`，交付与审查改用计数（老记录回退 diff）。
    定向 **252 passed**（147 + 41 + 64），`git diff --check` 0。
    关键事实：模型上下文里的写工具 diff 早已被 `agent/payload.py` 的
    `DROP_DIFF_TOOLS` 丢弃，本步主要减小 `tool.completed` / 检查点体积，
    并堵住 `repl` 等旁路；token 目标仍需「文件状态快照 / 压调用次数」。
    真机 benchmark 未跑（后端未启动），见 `ops/HANDOFFS/T-R2-01-claude.md`。
  - **2026-11-24 T-BA 会话批量归档**：侧栏「管理」选择态、一次整批确认、
    底部操作条与「已归档 N」折叠组；新增 `POST /threads/batch-archive`
    （archived/unchanged/missing，去重、空/超限 422、幂等）；两个会话列表接口
    支持 `status=all|active|archived`（先过滤后 limit）；已归档行「取消归档」；
    首页、全站左栏、任务页会话选择器只取活跃会话。顺带修复 `updateThread`
    缺 JSON content-type 导致取消归档/重命名 422。后端定向 **28 + 14 passed**；
    前端 **495 passed**、tsc/eslint 0；`check.sh --no-smoke` 全部通过；
    浏览器探针 1440×900 **26 步**、1024×768 **9 步**全过，console 0；
    真实接口确认归档后 active 不返回、archived 返回；验收项目已删除。
    截图 `data/batch-archive-*.png`。
    **2026-11-24 质量验收整改**：核对代码后确认 `DELETE /threads/{id}` 只把
    `status` 改成 `archived`，不删线程/轮次/事件/检查点；物理删除是独立的
    `DELETE /threads/{id}/permanent`，验收单「归档会物理删除」的判断不成立，
    没有发生数据丢失。为消除歧义，单条归档改走显式
    `PATCH /threads/{id}` `{status:"archived"}`，与批量归档一致；批量 docstring、
    `archiveThreads` 注释与交接单统一为「PATCH 改 status；物理删除走 /permanent」；
    两条活跃会话设置不同 `updated_at` 修复排序不稳定。复跑后端组合 **42 passed**、
    前端 `src/**/*.test.ts` **492 passed**、tsc/eslint 0。已归档达到取数上限
    200 条时，侧栏显示「仅显示最近 200 条已归档会话」（产品选 A，不改接口；
    分页留作后续）。
  - **2026-11-24 E12 工作区边界与整机检查/清理**：默认/计划/接受编辑仍拒绝
    工作区外路径；完全访问下跨盘只读自动放行、写/删逐次弹 `scope:outside_write`
    审批，批准后只执行该精确动作。清理先只读盘点、列清单、等明确同意，
    系统/用户/缓存不自动删；系统盘点先查固定位置、单目录 60 秒、不在 C 盘根目录
    写扫描文件；`run_shell` 拦住 C:\Users/C:\/C:\Windows 的整树递归，直接返回
    换路提示。定向 **58 passed**、前端 **486 passed**、lint/tsc 0；
    真实任务：跨盘盘点出 UserTemp 84,004 文件 / 4.20 GiB；跨盘清理批准一次后
    old.tmp 真删除；无目录检查电脑不编造。`C:\Windows\Temp` 需管理员权限。
    真机截图里的卡住任务（`dir /s C:\Users` 超时、往 C 盘根写扫描文件）已取消。
    2026-11-24 补：真机点「批准」报 HTTP 500 的根因是 Next rewrite 代理默认
    30 秒超时（审批同步恢复这次用了 52 秒），审批与命令实际已成功；已把
    `experimental.proxyTimeout` 放宽到 15 分钟，`tsc` 0、Next dev 重启后首页 200。
  - **2026-11-24 E11 所有产出物去 AI 味**：全局风格段新增产出物规范，覆盖文档 /
    网页 / PPT / 报告；禁止套模板、空泛开场、流行词、编数据、满篇 bullet、emoji、
    AI 仪表盘与三栏图标 PPT；图片/图表由 agent 自主判断（真实数据图/流程图/真实
    截图才加，纯装饰不加、不编造截图、不留占位）；审查清单新增第 6 条；可选
    「打开看看/改脚本重跑」不再判失败。定向测试 **94 passed**；真实 PPT 4 页
    succeeded；带数据 PPT 自主加原生图表 + 表格，数值与给定一致，缺失分店数据
    显示「—」；docx 缺真实信息时用「待补充」占位、不编数据。
  - **2026-11-24 主库基准数据清理**：删除主库里 19 个 `bench-*` 项目及其任务/会话，
    剩余 6 个非基准项目；工作区目录未动。截图里的 `data.csv` 失败不是模型没做完：
    该项目活动工作区是 `HANDOFFS`，确实没有 `data.csv`，agent 已递归搜索后如实报告。
  - **2026-11-24 E9 验收前置修正 + 子目录改动误报**：新建演示项目「E9验收演示」
    （工作区 `demo/e9`，含 data.csv 等 fixture），真跑 data.csv 任务 **succeeded
    / 18s**，结论 3 天 5 条、合计 800、均值 160.00，data.csv 未变；
    修复任务在仓库子目录时把仓库别处删除算进本任务的问题（`theme-toggle.tsx` 误报），
    定向 **8 passed**。
  - **2026-11-24 E10 模型选择只显示已配置供应商**：新增共用 `configuredProviders`
    （启用 + 有 Key），应用到聊天输入框、全站切换器、任务创建、角色模型、
    头脑风暴、旧聊天首页与任务页展示；设置页供应商配置列表保留全部。
    前端 **484 passed**、tsc/eslint 0；浏览器探针 3/3 断言、console 0，
    截图 `data/model-filter-only-configured.png`。
  - **2026-11-23 E9 日常基准 + 动作/交付修复**：新增 daily-001/002/003 三道日常题
    （隐藏测试判分）；DeepSeek **3/3 PASS**（pass@1=1，误改 0，平均 30.1s /
    397,637 token），Codex 同题也 **3/3 PASS**（45/168/92s，合计 446,734 token；
    daily-002 留 2 个临时脚本）。同时修复真实根因：文件白名单「其余文件不要改动」
    被误判为整轮只读，导致模型只查看不动手；确定性推回执行；shell 产物计入
    改动清单；报告条数不再出现「改动 1 个文件」自相矛盾；只读 shell 命令自动放行，
    日常整理审批卡 4→1（只剩 mkdir/copy）。用户已确认保留只读自动放行（2026-11-24）。
    相关定向测试 **113 passed**。
  - **2026-11-23 全量回归（已完成）**：复跑 **5090 passed / 28 skipped / 0 failed**
    （24:44，`logs/pytest-full-2026-11-23c.log`）。
  - **2026-11-24 全量补跑（第一次）**：**5094 passed / 28 skipped / 6 failed**
    （`logs/pytest-full-2026-11-24a.log`）。6 项都来自只读自动放行与既有断言冲突
    （`git status`/`echo` 原本被断言为 ASK，以及「每条命令都先确认」被只读规则
    盖过）。已修 `authorization.py` 让 confirm_all 优先于只读放行，并把两处样本
    改成写命令；相关测试 **101 + 30 passed**。
  - **2026-11-24 全量补跑（修复后）**：**5100 passed / 28 skipped / 0 failed**
    （22:23，`logs/pytest-full-2026-11-24b.log`）。E9 后端验证闭环完成。
    （此前 d/f/g 轮在权限改动前启动或被内存回收，已停止，不作数。）
  - **2026-11-23 第一次全量**：**5088 passed / 28 skipped /
    1 failed**——唯一红项是工具说明预算（22,856 > 22,700，由 write_file append
    说明引入）。已压缩说明，`test_tool_budget_v427.py` **5 passed**；全量复跑
    已启动（`logs/pytest-full-2026-11-23c.log`），结果出来后补记。
  - **2026-11-23 E8 日常场景对标 Codex**：日常提示词收紧为 Codex 式四条
    （直接动手少问、最简做法、做完核对、先给结论），默认简体中文；
    新对话默认场景改为「日常」（存过的选择仍优先）。场景/提示词 **24 passed**、
    前端 **481 passed**、tsc/eslint 0；新会话场景选择器显示「日常」，console 0。
  - **2026-11-23 D11 结果面板精简**：删除改动视图的面板级标题行、
    「本会话/搜索文件/分组」行与「变更/证据/活动」分段，改为顶部视图标签直接进入
    文件条 + diff；证据/活动在对话面板内不再可达（任务页仍有）。前端 **481 passed**、
    tsc/eslint 0；浏览器断言无工具栏且文件条/diff 正常，console 0。
  - **2026-11-23 D10 结果面板文件条上移**：窄面板下顶部四个视图标签按面板宽度
    隐藏数量/文字（460px 下均为 62×28 单行，不再竖排）；改动视图文件列表从左侧
    移到代码区上方，横向可滚动，溢出时出现左右箭头按钮，选中项自动滚入视野。
    前端 **481 passed**、tsc/eslint 0；460px 面板与左右箭头断言全部通过，console 0；
    截图 `data/e9-panel-top-files.png`、`data/e9-file-arrows.png`。
  - **2026-11-23 D9 任务筛选状态进地址栏**：任务页搜索词与状态筛选写入 URL
    （replaceState、保留其他参数），刷新/收藏/从详情返回后恢复；非法状态与空白词
    忽略。补记：任务页左下角模型显示改认后端默认（有 Key + is_default），
    不再显示没配 Key 的 anthropic。前端 **481 passed**、tsc/eslint 0；
    浏览器探针刷新后输入「计划书」与「未成功」筛选均恢复，
    左下角 `hasDeepseek=true / hasClaude=false`，console 0；
    截图 `data/e8-task-filters-footer.png`。
  - **2026-11-23 E7 默认模型设置入口**：设置页「模型」面板新增默认模型选择器，
    `PUT /providers/default` 校验存在/启用/有 Key；全站模型切换器改认
    `is_default` 与「有 Key」，不再显示没配 Key 的模板厂商；编辑面板首次落在默认供应商。
    接口单测 **4 passed**、前端 **476 passed**、tsc/eslint 0；两个选择器均显示
    `DeepSeek · deepseek-flash`，console 0；截图 `data/e7-default-selector3.png`。
  - **2026-11-23 E6 国内模型默认与包镜像**：默认供应商锁定 **DeepSeek**（设置文件），
    供应商列表返回 `is_default`，前端「默认模型」按它显示（不再拿列表第一项猜）；
    子进程默认 pip/uv → 清华 PyPI、npm → npmmirror，显式配置不覆盖、
    `BLANK_DOMESTIC_MIRRORS=0` 可关；办公文档默认 `.docx`。
    后端相关 **13 + 66 passed**、前端 **475 passed**、tsc/eslint 0；
    `/providers` 标记正确、模型选择器显示 `deepseek-flash`、console 0。
  - **2026-11-23 E5 文档产物预览**：结果面板打开 `.docx/.xlsx/.pptx` 时，后端抽取
    文本层（标题/段落/表格/工作表/幻灯片）返回 markdown，前端复用现有渲染器并标明
    「样式与图表已简化」；坏文件保持二进制回执。真实产物「数据智能分析平台-项目计划书.docx」
    抽取 **15,277 字**（94 段 / 12 个表格），浏览器断言通过、console 0；
    预览单测 **5 passed**、前端 **473 passed**、tsc/eslint 0；截图 `data/e5-office-preview.png`。
  - **2026-11-23 E4 文档库随包预装**：`python-docx 1.2.0 / openpyxl 3.1.5 /
    python-pptx 1.0.2` 纳入 `pyproject.toml` + `uv.lock`；开发 venv 与
    `desktop/build/runtime` 均已安装（运行时重建 **69 包 / 153.3MB**，冒烟导入含三库）；
    环境预检新增「应用解释器」并报文档库可用；单进程超时 3s→6s（冷启动 lxml/Pillow）。
    三库真实生成 docx/xlsx/pptx（36633 / 4828 / 28213 字节）；`uv lock --check` 通过；
    后端重启 health 200、前端 console 0。桌面安装包需重新 `npm run dist` 才带上新库。
  - **2026-11-23 E3 长文件分段写入**：`write_file` 新增 `append`（宿主与 fs-broker
    两条路径共用），处理 BOM/CRLF/无尾换行接缝、空追加不变、二进制拒绝；
    工具说明与编程场景提示要求超长内容分节追加。追加 + 既有写入套件 **82 passed**、
    含场景提示 **48 passed**；schema 检查通过；后端重启 health 200、前端 console 0。
  - **2026-11-23 E2 开局环境预检**：任务开局探测 python / `py -3`（含 pip）/ `.venv`
    / node / git / uv，并顺带读 python-docx / openpyxl / python-pptx；结果缓存
    5 分钟并注入系统提示（只报版本、不报路径），写 `environment.profiled` 事件；
    纯聊天不探测、失败不阻塞。预检单测 **5 passed**、含 runtime 路径的相关套件
    **53 passed**；本机实测 **702–833ms**（py -3 3.12.10 + pip 25.0.1、node 24.20.0、
    git 2.56.1、文档库均未安装）；后端重启 health 200、前端 console 0。
  - **2026-11-23 E1 执行环境与目标状态修复**：修复真实任务「不停失败」的根因——
    `py -3` 不再被改写成精简解释器；缺 pip / 误用 POSIX 管道时回执直接给替代做法；
    run_shell 说明按 cmd 生成（不再教 `;` 串联）；恢复执行时目标状态回到 executing；
    编程场景提示文档默认 Markdown、不反复装依赖。补记：发送栏常驻提示点名
    「首次装依赖、内联脚本」仍会审批；代理吐出的无结构化 5xx 改显示
    「后端暂时不可用…可能正在重启」并带请求路径。shell 相关 **53 passed**、
    场景/提示 **75 passed**、目标回写 **2 passed**、前端 **473 passed**；
    真实命令 `py -3` 退出码 0、`py -3 -m pip` 可用；后端重启后 health 200、
    前端 console 0。
  - **2026-11-23 D8 界面状态记忆**：侧栏收窄 / 结果面板开合写入本地偏好并在启动
    数据返回后读回；会话滚动位置按 thread id 记在内存，离开前保存、切回时还原
    （原本贴底则继续贴底）；结果面板按会话重挂载（`key=threadId`），切换会话不再
    残留上一条会话的文件标签。浏览器探针：侧栏收起后 reload 仍收起、面板收起后
    reload 仍收起且可再打开、长会话滚到 300px 切走再切回仍为 300px、文件标签跨会话
    不残留，断言全部通过，console 0；前端 **469 passed**、tsc/eslint 0。
  - **2026-11-23 D7 结果面板重设计 + 无产出隐藏 + 横向滚动**：普通对话无产物/
    改动/证据时右侧面板与入口不渲染（「你好」会话断言无 separator）；面板默认
    620px、四视图固定 pill、文件标签独立一行、工具栏精简；diff/文本预览改
    `whitespace-pre` + 行容器 `w-max min-w-full`、行 `w-full`，实测
    `scrollWidth 961 > clientWidth 444`、`scrollLeft 200` 断言通过；补记修复
    「拖到右边行底色消失」：行宽全部 961px、`scrollLeft 400` 处命中行底色，
    断言通过，截图 `data/d7-bg-fixed.png`；前端 **469 passed**、tsc/eslint 0、
    journey 通过；浏览器探针 console 0。
  - **2026-11-23 D6 全页面玻璃一致性 + 丝滑 v1**：12 个页面浏览器审计
    **console 0 / 无失败请求**；任务页补外层玻璃卡片；会话新增「回到底部」
    （1280×600 实测出现并可平滑回底）、加载骨架屏、页面入场动画；
    前端 **469 passed**、tsc/eslint 0、journey 通过。
  - **2026-11-23 D5 液态玻璃皮肤 v1**：**只保留一套浅色液态玻璃**，
    深色主题/切换/`theme-toggle.tsx` 已移除；极光背景、镜面高光、玻璃细颗粒、
    内层折射与彩色主按钮；前端 **469 passed**、tsc/eslint 0、journey 通过；
    首页/设置/会话浏览器探针 **console 0**；目录选择框点开后 `[role=dialog]` 断言通过。
    修复了侧栏文字发糊（改 `glass-side`）和 portal 被 `body > *` 覆盖导致目录框点不开。
    本地运行：前端 :3000、后端 :8000。全部页面走查留待下一轮。
  - **2026-11-23 K2 会话授权撤销**：列出/单类撤销/全部撤销本会话授权；
    会话条「授权 N」入口；后端 **4 passed**、前端 **469 passed**、
    tsc/eslint 0、journey 通过；OpenAPI 135 paths 含 2 条 grants 路由。
    授权仍只从审批卡写入，撤销只影响未来判定。
  - **2026-11-23 K1 自学习可见闭环**：经验/偏好快照、逐条退役、候选偏好采用、
    体检告警；设置页「记忆 → 自学习」；后端 **5 passed**、前端 **467 passed**、
    tsc/eslint 0、journey 通过；OpenAPI 133 paths 含 4 个 learning 端点。
    不改自动召回/注入策略，技能提案仍人工采用。
  - **2026-11-23 U1 结果状态条**：过程摘要行增加完成/失败/等你批准等状态词，
    失败段标红，未知终态显示「已结束」；前端 **464 passed**、tsc/eslint 0、
    journey 通过。工具调用行化现状已是每步一行、可展开，本卡未改。
  - **2026-11-23 B3c 多文件应用预览**：按需启动 `node <文件>` 服务，
    只绑 127.0.0.1、Job Object、10 分钟 TTL、进程树回收；前端预览 Tab；
    后端 **22 passed**（含真实 node 启动/复用/停止）、前端 **461 passed**、
    tsc/eslint 0、journey 通过；OpenAPI 129 paths 含 preview 端点。
    `BLANK_SANDBOX_ALLOW_LOCAL=0` 时 409 拒绝，不降级。
  - **2026-11-23 B3b 单文件内嵌预览**：HTML 用最严格 sandbox 静态预览、
    Markdown 复用现有渲染器、JSON 格式化、CSV/TSV 表格与截断提示；
    前端 **458 passed**、tsc/eslint 0、journey 通过。HTML 刻意不执行脚本。
  - **2026-11-23 B3a 产物面板增强**：产物视图搜索、按类型分组、复制路径、
    导出 JSON/Markdown；前端 **452 passed**、tsc/eslint 0、journey 通过。
    纯前端，未改后端。
  - **2026-11-23 Y1 成本闸设置入口**：`/settings/agent` 返回并接受
    `budget`（金额上限 + 价目表），设置页新增「成本与预算」面板；
    后端相关 **30 passed**、前端 **447 passed**、tsc/eslint 0、journey 通过。
    金额闸默认仍为关（0 = 不限），本卡不做自动省钱。
  - **2026-11-23 人工验收（B10）**：B10 自身三处修复、B7 证据、journey、tsc、
    eslint、后端 75 条均通过；首次门禁因前端单测 1 红不通过
    （`transition-copy.test.ts:124` 缺 `list_only` / `ui_repair_reserve` 登记，
    来自工作区未提交的 `transition.py` 改动，非 B10 引入）。
    X1 补齐登记后复跑 **443 passed / 0 failed**、tsc 0、journey 通过；
    记录见 `docs/validation/acceptance-2026-11-21.md` 末尾 B10 节。
    仍待用户人工最终确认。
  - **B10 browser_check 动作可靠化**：click/type 后自动 settle 600ms、长页面
    先 scrollIntoView、steps 的 `assert` 支持 `{selector,min}`；用 B7 模型
    同款“无 wait”步骤复测 **7/7 动作通过、5/5 expect、控制台 0 错误**。
    **完整 B7 从头复跑 succeeded**：28 步 / 32 调用 / **1,757,693 token** /
    16 变更 / `npm test` **17 passed** / browser_check 2 次 / 验收 3/3；
    成本高但真实任务第一次完整通过。
  - **B9 确定性 UI 冒烟**：新增 `ui_smoke.py`，模型完全不调用 browser_check
    时，验证节点自己读 `package.json` 的 `node <file>` start，起随机回环端口，
    用 `browser_probe` 做打开 + reload + 正文非空 + 控制台零错误；真机对 B7
    工作区返回 `passed`（1 assert / 2 steps / 0 console），服务用完即杀。
    单元测试 60 passed；完整 B7 在 B9 下从头复跑仍 **failed**（25 步 /
    32 调用 / 1.709M token）：模型这次调用了 browser_check，但 click/type
    后没有 wait，两条异步断言失败；自动冒烟只覆盖“完全没跑”，不覆盖
    “跑了但写错”；
  - **B8 UI 闸门自修**：预算耗尽时保留一次 UI 修复，该回合允许调用
    browser_check/run_shell；B7 复跑里 `ui_repair_reserve` 生效并调用 6 次
    browser_check，但模型自己的断言写错（初始断言聊天消息数为 1），仍未通过。
    用明确 UI 自检指令的小任务（预算未耗尽）**succeeded**：19 步 / 21 调用 /
    844K token，9 expect + 6 动作通过，控制台 0 错误；独立探针 10/10 通过。
    但最终提示词下从头复跑完整 B7 **仍 failed**：28 步 / 33 调用 / 1.67M token，
    `reserved_ui_repair=true` 但模型一次 browser_check 都没调用；下一步必须做
    确定性强制/自动探测，不能继续只加提示；
  - **B7 真实多文件任务**：第一轮因“不要修改工作区之外的文件”被误判只读、
    **0 改动却 succeeded**；已修 `directives.py` 的范围约束识别并补回归
    （98 passed）；第二轮真正开始搭建，26 步 / 29 调用 / **1.74M token** /
    12 文件 / 9 个接口测试全过；3/3 验收 `covered=true`，但 **ui_check 失败**
    （前端改动后没跑 browser_check），修复被 `budget_exhausted` 截断。
    独立浏览器 13/13 动作断言通过，仅 `/favicon.ico` 404。结论：能写但没自检、
    预算不够修；B8 方向是 UI 闸门自修/保留修复预算/favicon；
  - **B6 T25 成本**：从 B5 的 **399K token / 16 轮 / 43s** 降到
    **136–156K / 9–10 轮 / 16–23s**，隐藏验收仍 100 分；与 Claude 138K
    基本持平，轮次更少；
  - **根因**：链式命令 `set "PYTHONPATH=src" && python ...` 的第二段没被
    替换（Windows 9009）→ 模型 where/py 重试；34 个工具说明书每轮 22,467
    字符；文档任务没有执行纪律，读全仓/写临时脚本/查 git；
  - **修复**：`normalize_python_command` 按段替换 + 无 venv 回退
    `sys.executable` + 显式 PATH 不覆盖 + Docker 不回退；文档-only
    `documentation_only` 收窄工具（34→16）并追加执行纪律；
  - 相关后端回归 **161 passed**（含 6 条解释器与 1 条文档-only 新用例）；
  - 真机两次 T25：156K/10 轮/23s、136K/9 轮/16s，均只改 README、
    隐藏验收通过；
  - T01 普通编程回归：**155K / 8 轮 / 13s / 100 分**，与 B5 基本一致，
    文档-only 收窄未误伤；
  - 四任务最终复跑：ours **98.7** / Claude **94.4**，双方 4/4；
    ours 166K/19s vs Claude 154K/26s；T25 135K vs Claude 178K；
    Claude 的 T07/T25 自证未检测到，94.4 是下限；B6 运行时旧版 Codex 不可用；
  - **B6 后端全量**：**5033 passed / 28 skipped / 0 failed**（18:44，退出码 0）；
  - **B5 既有**：四任务对标 ours 96.5 / Claude 97.5，双方 4/4；当时旧版 Codex 不可用；
  - 后端全量 **5024 passed / 1 failed / 28 skipped**；唯一失败是 B4 改系统
    提示后未同步的哈希，已更新并单跑 `test_output_styles_v47.py` **31 passed**
    （全量未在改哈希后重跑）；
  - 前端 ESLint 0、`tsc` 0、单测 **443 passed**；`check.sh` 全部通过；
    `pnpm run build` 退出码 0（CSP 17/32）；
  - D4 收尾：前后端依赖审计 0、严格 CSP、后端全量 **5001 passed / 0 failed**；
  - 会话永久删除（AgentEvent/AgentCheckpoint/SearchDoc）已修复，测试 23 passed；
    今日测试会话已永久清理。
- 下一步：
  1. ARCH-01 收口：移 `_run_request`/`launch_task`/`_enqueue_and_maybe_run`/
     `recover_*`，然后跑一次全量（排除 3 项 browser 环境红）。
  2. 人工验收 ARCH-01a/01b/01c/ARCH-05/ARCH-06b：真机创建任务、失败/通知、
     并发两条任务、外键级联删除；非提权终端复跑全量确认 0 failed。
  3. 继续 Phase 2：ARCH-04（AgentRun 1:N）、ARCH-02（拆 graph.py）、
     ARCH-03（统一入口）、ARCH-07（团队任务入库）。
  4. browser_probe 残留已修，后续全量不再需要人工清 Chrome。
- 未处理：
  - ARCH-05/ARCH-06b 真机并发与外键级联删除验收未做；全量已验证。
  - `browser_check` 3 个真跑用例在提权 pytest 下仍为环境红；进程残留已修。
  - `foreign_keys=ON` 首轮 63 failed 的根因是测试夹具只造子行；已修 18 个文件，
    全量通过，但生产删除路径仍建议真机复核。
  - `browser_check` 3 个真跑用例在提权 pytest 下为环境红：pytest 临时目录 ACL
    收紧到 owner-only，Chrome 非提权沙箱读不到页面；对照探针打开仓库 HTML 正常。
  - BUG-01（broker 读图）与 BUG-05（CREATE_SUSPENDED 挂 Job）已在批次 1B 修复，
    见 `ops/HANDOFFS/T-LAUNCH-1B-claude.md`；broker 大图 IPC 体积与恢复失败路径
    仍需真机验收。
  - B7 只有一次成功运行、一个模型、一次 bypass 自动化；没有正常审批、没有
    Claude/Codex 同题对照；
  - B9 自动冒烟只覆盖“模型完全没跑 browser_check”；模型跑了但断言/时序
    写错仍会阻塞；
  - B10 默认 settle 600ms 对慢接口可能不够；AI 巡检需要更长 wait 或更高 settle；
  - B7 成功 run 的 1.76M token 成本很高；大任务成本问题未解；
  - B6 验证 T25 与 T01 回归；T03/T07 未重跑完整对标；
  - 文档-only 依赖显式边界，没写“不改源码/只改文档”的文档任务仍走全量，
    可能继续高成本；
  - 文档-only 任务若实际需要浏览器/git，这些工具被排除；不应把这类任务
    标成“只改文档、不改源码”；
  - B6 运行时 Codex 仍是旧 0.154 + nex（404/402）；之后更新到 v0.171.0 +
    spark-x2.6，**交互式 TUI 正常**（node_repl 读文件/校验），但 `codex exec`
    自动化 T01 在 workspace-write/danger-full-access 下都 300s 超时、
    apply_patch 写出语法错误；这是自动化入口/终端链路问题，尚不能算有效编码
    对比，TUI 可用于人工只读审查；
  - Claude Code 自证只能从最终文本判断，B5 的 T25 90 分是下限；
  - B5 只有 4 个小任务，隐藏验收由本仓库编写，不能据此宣称整体超过 Claude；
  - 文档 / 头脑风暴 / 逆向不开放新建（产品决定），只留给老任务；
  - 未做 WorkBuddy 的仓库/分支启动器与连接应用引导；
  - 动态 SSR 页面不在构建期哈希 CSP 覆盖范围；
  - thread/task 没有记录 workspace：旧对话只能跟随项目当前目录，精确还原需要
    数据库变更；
  - 否定式指令目前是规则式，复杂语义否定（“不是不能改，但先别动”）认不出时
    仍按原链路走；只读任务的工具表仍列写工具（执行层硬拒），下一步可在装配层收窄；
  - 场景策略首版只有规划提示、验证提示、预算比例真实生效；模型路由/上下文/
    日常应用集成未做；
  - 临时验证工作区与任务（`data/_ui_gate_probe*`、`5a814453`、`c0ceba72`）保留；
    `_bench_workspaces` 已逐个清理，只剩空目录。
