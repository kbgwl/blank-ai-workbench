# E9 日常基准 + 动作/交付修复 开发交接单

## 任务与范围
- 已实现：
  - 新增日常场景基准 `daily-001/002/003`：文件分类复制、python-docx 生成文档、
    CSV 按天汇总；各题带 fixture 与隐藏测试，复用 `coding/run.mjs` 判分。
  - `run.mjs` 支持 `--tasks-dir / --fixtures-dir`、任务 `scenario` 透传；
    工作区快照改为递归并跳过缓存/`.blank`，子目录产物能被计入判分。
  - 修复「只查看、不动手」的真实根因：约束句「其余文件（尤其测试文件）一律
    不要改动」与「不要修改 data.csv」被 `directive_mode` 当成整轮只读，
    导致基准任务 0 文件改动却 succeeded。现按文件白名单 / 单文件保护放行。
  - 执行节点新增确定性推回：计划里有 edit/repair 步骤、模型却只读不写时，
    最多推两轮并要求直接调用工具；动作发生或达到上限后放行。
  - 交付改动清单补入 shell 产物：收尾前用任务快照比对工作区，
    `copy` / `python` 生成的文件不再漏计；文本、结构化负载、闭合记录同源。
  - 交付文案：「需要你」里的「无需处理 / 无需后续操作」等无交办句不再展示；
    结论要求有统计结果时先写关键数字；可选的改进建议不再写进「下一步」。
  - 只读 shell 命令（dir / type / fc / cd / git status / git diff 等）自动放行，
    不再逐条挂审批卡；日常文件整理从 4 张卡降到 1 张（只剩 mkdir/copy 这类写动作）。
    重定向、变量展开、绝对路径/盘符路径、写/删除/推送命令仍然逐条问。
  - 修复开局环境预检 `run_root` NameError（改为 `session.context.root`）。
- 明确未实现：
  - 未重跑编程基准全量；`directive_mode` 修复会解除编程基准同样的只读误判，
    但需要下一轮基准读数确认。
  - Codex 对照中 daily-002 留下两个临时脚本（`_make_docx.py`、`_verify_docx.py`），
    我方无额外文件；判分仍按隐藏测试与文件边界分别记录。
  - 未新增前端代码；交付格式仍是既有四行口径。

## E 盘绑定（2026-11-24）
- 评测工作区：`run.mjs` 的 `WORK_ROOT` 固定为项目内 `.bench-tmp/`（E 盘），
  不再使用 Windows 默认 `%TEMP%`（C 盘）；可用 `BLANK_BENCH_WORK_ROOT` 覆盖。
- 测试临时目录：后续 pytest 命令统一设 `TEMP=TMP=E:\projects\kbgai\kbgai\.tmp-test`。
  已用该方式跑 `test_directives_v496.py`：**14 passed**，临时目录落在 E 盘。
- 后端运行临时目录：主后端以 `TEMP=TMP=E:\projects\kbgai\kbgai\.tmp` 重启；
  `UV_CACHE_DIR=E:\env\uv-cache`。
- 启动入口：`desktop/main.cjs` 给后端子进程注入 `TEMP/TMP=E:\env\blank-tmp`、
  `UV_CACHE_DIR=E:\env\uv-cache`；`scripts/dev.mjs` 同样在 Windows + E 盘存在时生效。
- Codex 对照现场：`evals/benchmarks/daily/codex-runs/`（E 盘，已 gitignore）。
- `.bench-tmp/`、`.tmp/`、`.tmp-test/` 已加入 `.gitignore`。
- 已按用户确认清理 C 盘 `%TEMP%` 下本项目产生的 `blank-bench-*`、`blank-control-*`、
  `pytest-of-22103` 共 **1,347 个目录 / 约 1.33GB**；未触碰其他临时目录。

## 修改内容
- `evals/benchmarks/coding/run.mjs`：任务/fixture 目录参数、scenario 透传、
  递归快照与缓存/`.blank` 跳过。
- `evals/benchmarks/daily/`：3 个任务 JSON、fixture、隐藏测试。
- `services/api/src/blank_api/services/agent/directives.py`：文件白名单与
  单文件保护不再被认成整轮只读。
- `services/api/src/blank_api/services/agent/graph.py`：只看不动手推回、
  快照比对 shell 产物、交付无意义提示过滤、结论数字要求、校验 skip 口径。
- `services/api/src/blank_api/services/sandbox/snapshot.py`：新增
  `changed_since()`（新增/修改/删除，尺寸+内容比对）。
- `services/api/src/blank_api/services/agent/runtime.py`：环境预检根路径修复。
- `services/api/src/blank_api/services/agent/intent.py`：交付结论要求关键数字。
- `services/api/src/blank_api/services/permissions.py`：新增只读命令识别与自动放行，
  排在显式 ask / 不可逆 / 越界 / allowlist 策略之后。
- 测试：`test_agent_continuation_v25.py`、`test_directives_v496.py`、
  `test_delivery_changed_paths_v488.py`、`test_validation_gate.py`、
  `test_permission_rules_v112.py`。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_delivery_changed_paths_v488.py tests/test_directives_v496.py tests/test_agent_continuation_v25.py tests/test_agent_remediation_v24.py tests/test_readonly_and_budget.py` | 57 passed | 交付/指令/推回 |
| `pytest tests/test_validation_gate.py` | 19 passed | 校验 skip 口径 |
| `pytest tests/test_permission_rules_v112.py tests/test_permission_modes_v11.py tests/test_permission_profile_v48.py tests/test_settings_permissions_v50.py` | 101 passed | 只读命令放行与权限模式 |
| 日常基准（只读放行后） | 1/1 PASS、**approvals=1** | 只剩 mkdir/copy 写动作一张卡 |
| `pytest tests/test_product_gaps_ui_v166.py tests/test_intent_and_reply.py` 等 | 63 passed | 提示词与交付 |
| 日常基准（DeepSeek，隐藏测试） | **3/3 PASS** | pass@1=1，误改 0；30.2s / 40.5s / 19.4s，合计 397,637 token |
| Codex 同题（spark-x2.6） | **3/3 PASS** | 45s / 168s / 92s，合计 446,734 token；daily-002 留 2 个临时脚本 |
| 后端全量（改动前基线） | 5090 passed / 28 skipped / 0 failed | 24:44 |
| 后端全量（第一次补跑） | **5094 passed / 28 skipped / 6 failed** | 6 项均为只读自动放行与既有断言冲突；已修复 `authorization` 对「每条命令都确认」的优先级并更新两处样本 |
| 后端全量（修复后） | **5100 passed / 28 skipped / 0 failed** | 22:23，`logs/pytest-full-2026-11-24b.log` |
| 演示项目 `E9验收演示` | **succeeded / 18s** | 3 天 5 条、合计 800、均值 160.00；`data.csv` 未变 |
| `test_delivery_changed_paths_v488.py` | **8 passed** | 新增：子目录工作区不把仓库别处删除算进本任务 |
| E 盘 TEMP 冒烟 | 14 passed | `test_directives_v496.py`，临时目录落 E 盘 |
| 桌面壳单测 | **104 passed** | 含启动接线、解释器优先级、真实启动/迁移路径 |

基准证据：
- 我方最终读数：`report-deepseek-001-003-final.json` + `report-deepseek-002-final2.json`
- 交付对比：`report-deepseek-daily-all-2.json`
- `evals/benchmarks/daily/codex-runs/*/last.txt`（Codex 交付文本与判分现场）
- 同机同日对比：我方平均 30.1s / 397,637 token，Codex 平均 101.7s /
  446,734 token；双方 3/3 通过，我方无额外文件。

## 风险与需要审查的点
- `directive_mode` 放行规则基于明确句式（其余文件…不要改动 / 不要修改 `<带扩展名文件>`）；
  更隐晦的作用域约束仍可能误判为只读，需真实任务继续抽样。
- 任务跑在 git 仓库子目录时，`git status` 返回 repo 根相对路径；已按工作区前缀
  过滤，仓库别处的删除不再算进本任务（真机：`theme-toggle.tsx` 误报已修）。
- 快照比对只在任务里出现过成功 `run_shell/monitor` 时执行，会多一次工作区遍历；
  大工作区受快照扫描预算限制，超限时退回工具回执口径（不会阻断任务）。
- 四行交付的「验证」仍只认带退出码的自动化测试；日常任务常见「读回核对」
  只体现在结论文字里，不占「验证」行。
- Codex 对照使用的是其当前配置模型 spark-x2.6，不是固定基准；读数用于同机同日
  体感对比，不当作跨模型榜单。
- 本次改动了权限判定（只读 shell 自动放行），属于行为变化：默认模式下
  `dir/fc/cd/git status` 等不再挂卡。写/删除/推送/越界/显式 ask 规则仍然逐条问；
  安全设置里的「每条命令都先确认」优先于只读自动放行（`authorization.py` 已收口）。
  用户已确认保留只读自动放行（2026-11-24）。
- 日常文件整理仍会为 mkdir/copy 弹 1 张卡；要做到零卡片，需要再放行
  「工作区内 mkdir/copy」，那是更大的权限变更，本卡未做。
- 本轮评测曾误跑在主库 8000，留下 19 个 `bench-*` 项目；已按用户确认全部删除
  （任务/会话一并清理，工作区目录未动）。后续基准必须用独立评测库/8060，
  不得再往主库建任务。
- 桌面壳/`scripts/dev.mjs` 已注入 E 盘 TEMP 与 uv 缓存；其他平台或无 E 盘时
  保持系统默认。桌面壳单测 **104 passed**。

## 建议人工验收
> 前置：`data.csv` 任务必须在**含 data.csv 的工作区**里跑。
> 已准备演示项目「E9验收演示」，工作区 `E:\projects\kbgai\kbgai\demo\e9`，
> 内含 data.csv、说明.txt、发票/合同/会议纪要/logo.png。刷新侧栏后直接用这个项目。
> 不要在 `ops/HANDOFFS` 里跑 data.csv 任务——那里没有 data.csv。

1. 打开「E9验收演示」项目，发「读取 data.csv 生成汇总.md」，确认结论带
   天数/条数/合计/均值，`data.csv` 未被修改。
2. 在同一项目发「把这个目录里的文件按类型整理一下」，确认它直接创建
   `整理/` 目录、复制并写说明，交付改动数量与实际文件数一致。
3. 发一个明确要求「不要修改任何文件，只分析」的任务，确认仍被锁为只读，
   不会误改工作区。
