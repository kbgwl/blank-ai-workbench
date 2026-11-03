# WorkBuddy v162 验收报告：Super Agent 升级版

工作目录：`E:\projects\kbgai\kbgai`
任务书：`docs/workbuddy-v162-super-agent-upgrade.md`
基线版本：`0.8.17`（**未改版本号**——`check:versions` 已确认 0.8.17 全口径一致）

## 零、先说结论

本轮把可自动验证的门槛全部跑成真实数字：**§I–§V、§VII 全绿**；**§VI 55 项真实 Benchmark
已单次完整跑完**（不拼接、不删失败、不改判分器）；**§VIII 真实 Electron GUI 在本 headless
环境无法取证**。

**`v0.9.0 尚未达到发布条件`。** 有三处硬门槛未过（任一即足以拦截发布）：

| 门槛 | 实测 | 要求 | 判定 |
|---|---|---|---|
| completionRate | **89%**（49/55） | ≥90% | ✗ 差 1 例 |
| testPassRate | **95%**（52/55） | =100% | ✗ 3 例测试真失败/未跑成 |
| falseClaimCount | **2** | 0 | ✗ coding-036 / coding-042 |
| passRate | 93%（51/55） | ≥90% | ✓ |
| misEditedFiles | 0 | 0 | ✓ |
| falseDoneCount | 0 | 0 | ✓ |
| unauthorizedHighRisk | 0 | 0 | ✓ |
| totalTokens | 5,027,930 | >0 | ✓ |

加上 **§VIII 真实 GUI 真机闭环在本环境无法完成**，四条理由里任意一条都让发布不成立。

**v162 的实际进步也要如实记：** 相比 v161 终态 `last-run.json`（completion 87% / testPass 96% /
misEdited 1），本轮 **completion 87%→89%**、**misEditedFiles 1→0**（coding-039 修复生效），
但 **testPassRate 96%→95%** 略降，且新上线的 `detectFalseClaim` **首次把 2 例「成功却假称验证过」
的假完成翻了出来**（v161 抓不到）。也就是说：修复方向对，但能力与口径还没到发布线。

版本号维持 `0.8.17`，**未提交、未打 tag**。

---

## 一、v162 新增/改动文件归属（§I 工作区检查）

`git status --short` 实测：15 个 `M` + 13 个 `??`，全部为 v162 源码/测试/迁移/CLI/报告，
**无密钥、无数据库、无日志、无临时脚本泄漏**。`git diff --check` 仅 LF/CRLF 警告（Windows 正常）。

### 新增（P0 Super Agent 实体）
| 文件 | 职责 |
|---|---|
| `services/api/src/blank_api/services/agent/dispatch.py` | Router 快照：纯函数，意图/场景/优先级/模型/工具/通知/记忆策略 + 预算，快照哈希不可变 |
| `services/api/src/blank_api/services/agent/triggers.py` | 事件触发：dedupe_key 窗口去重、到期释放、过期清理 |
| `services/api/src/blank_api/services/agent/watchdog.py` | Watchdog 纯策略：心跳/无事件/重复失败/预算/缺证据 5 类诊断，绝不提权 |
| `services/api/src/blank_api/models/trigger.py` / `schemas/trigger.py` | 触发器模型 / schema |
| `services/api/src/blank_api/api/routes/triggers.py` | 触发器上报/查询/点火路由（受 `background_execution` 开关控制） |
| `services/api/alembic/versions/0031_dispatch_snapshot.py` | 任务表加 `dispatch_snapshot` 列 |
| `services/api/alembic/versions/0032_agent_triggers.py` | 触发器表 |
| `services/api/tests/test_dispatch_v162.py` | Router 安全默认值 |
| `services/api/tests/test_dispatch_runtime_v162.py` | 快照落库 + 执行侧强制 |
| `services/api/tests/test_triggers_v162.py` | 触发去重/到期/释放 |
| `services/api/tests/test_watchdog_v162.py` | Watchdog 纯策略 + 落库去重 |
| `services/api/tests/test_legacy_fixes_v162.py` | coding-008/039/042/014/§7.5 修复单测 |

### 新增（CLI 包）
| 文件 | 职责 |
|---|---|
| `packages/cli/package.json` | `blank-ai-cli` v0.1.0，bin `blank-ai` → `src/index.mjs`（零 npm 依赖） |
| `packages/cli/src/index.mjs` | `start`/`doctor`/`init`/`list`/`add`/`remove`；`start` 拉后端+静态界面+绑工作区+轮询 `/health` |
| `packages/cli/README.md` | CLI 说明 |

### 改动（既有）
`tasks.py`（快照落库+运行收紧+接线）、`routes/__init__.py`（注册 triggers）、`models/task.py`、
`schemas/task.py`、`agent/runtime.py`（读快照）、`agent/graph.py`（§7.5 空 answer 不回 JSON）、
`agent/intent.py`（临时脚本用完即删 + `derive_answer_fallback`）、`tools/shell.py`
（coding-008 venv 解释器 / coding-039 清临时脚本）、`worker.py`（释放到期触发器）、
`services/features.py`（`background_execution` 开关）、`evals/benchmarks/coding/run.mjs`
（noFalseClaim + falseClaimCount；断连归 provider_failed 重跑）、`evals/lib/judgement.mjs`
（`detectFalseClaim`）、`evals/tests/judgement.test.mjs`（5 例）、`README.md`。

---

## 二、架构说明（P0 实体）

```
用户输入 → POST /triggers（事件）或 POST /tasks（目标）
                │
        Router（dispatch.py）── 纯函数，产出不可变 ExecutionSnapshot
                │
        Worker（worker.py）── 唯一执行者，心跳续租 / 检查点幂等 / 断点恢复
                ▼
        AgentSession（graph.py）── 按快照工具边界执行
                │
        Watchdog（watchdog.py）── 心跳/无事件/重复失败/预算/缺证据 5 类诊断（绝不提权）
                │
        Triggers（triggers.py）── 事件去重 / 到期释放 / 过期清理
```
- **Router 只决策不执行**（纯函数，不查库不调模型）；**Worker 唯一执行**；**开关默认关闭**。

---

## 三、测试矩阵结果（§II / §III / §IV / §V 全绿，本轮真实重跑）

### 后端全量（§V）
```
cd services/api && CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest -q
```
**`2079 passed / 23 skipped / 0 failed`（62 warnings，815.39s，EXIT=0）。**
其中 §II（`test_legacy_fixes_v162`）、§III（`test_dispatch_v162` / `test_dispatch_runtime_v162`）、
§IV（`test_triggers_v162` / `test_watchdog_v162`）全部并入通过。

### 前端 / CLI / 桌面 / 评测门禁（§V 真实重跑）
| 套件 | 命令 | 结果 |
|---|---|---|
| evals | `node --test evals/tests/judgement.test.mjs` | **15 passed / 0 failed** |
| check:versions | `node scripts/check_versions.mjs` | **全部 ok**（0.8.17 全口径一致） |
| desktop | `node --test desktop/tests/*.test.cjs` | **57 passed / 0 failed** |
| web 单测 | `pnpm -C web test` | **262 passed / 0 failed** |
| web lint | `pnpm -C web lint` | **clean** |
| web typecheck | `pnpm -C web exec tsc --noEmit` | **clean（EXIT=0）** |

合计本轮重跑 **2079 + 15 + 57 + 262 = 2413 例通过，0 failed**；check:versions 一致；lint/typecheck 干净。

---

## 四、v161 遗留问题修复（§II 逐项，已被 §三 覆盖）

| 编号 | 做法 | 验证 |
|---|---|---|
| coding-008 | `shell.normalize_python_command`：裸 python 换工作区 venv | 单测 7 例，并入 2079 |
| coding-039 | `cleanup_scratch_files` + REPORT 指令「用完即删」 | 单测 3 例；**Benchmark 中 coding-039 通过、misEditedFiles=0 佐证修复生效** |
| coding-042 | `detectFalseClaim` + `noFalseClaim` + `falseClaimCount` | 单测 5 例；**Benchmark 实测抓到 2 例假完成（coding-036/042），证明检测生效** |
| coding-014 | 断连归 `provider_failed`，90s/180s 退避重跑 | 代码路径修正；本轮 8 例 429 重跑均恢复，providerFailed=0 |
| §7.5 | `derive_answer_fallback`：空 answer 不回原始 JSON | 单测 4 例，并入 2079 |

---

## 五、55 项 Benchmark（§VI，**已单次完整跑完**）

**执行方式（符合「单次完整、不拼接」）：** 独立评测后端 **8061**（`blank_eval.db` 副本 +
独立密钥，目录 `E:\tmp\blank-eval-8061`）；provider = `bench-bigmodel-glm45flash`
（`e898e100-…`，anthropic 网关，API 加密存储，非明文）；命令：
```
BLANK_BENCH_API=http://127.0.0.1:8061/api/v1 node evals/benchmarks/coding/run.mjs \
  --provider e898e100-... --pause 6 --report E:/tmp/blank-eval-8061/bench-55-bigmodel.json
```
55 个 fixture 单跑，分母恒 55；provider 限流按 90s/180s 重跑 2 次；不删失败、不改判分。

### 汇总（源自 `bench-55-bigmodel.json`）
| 指标 | 实测 | 阈值 | 判定 |
|---|---|---|---|
| total / judged | 55 / 55 | 55 | ✓ |
| setupFailed / runnerError / timedOut / providerFailed | 0 / 0 / 0 / 0 | 0 | ✓ |
| providerRetried（429 后重跑） | 8 | — | 说明 |
| **completionRate** | **89%**（49/55） | ≥90% | **✗** |
| **passRate** | 93%（51/55） | ≥90% | ✓ |
| **testPassRate** | **95%**（52/55） | =100% | **✗** |
| **misEditedFiles** | 0 | 0 | ✓ |
| **falseDoneCount** | 0 | 0 | ✓ |
| **falseClaimCount** | **2** | 0 | **✗** |
| **unauthorizedHighRisk** | 0 | 0 | ✓ |
| **totalTokens** | 5,027,930 | >0 | ✓ |

状态分布：`succeeded 49 / failed 1 / cancelled 5`（5 例 cancel_resume 任务预期 cancelled，全部命中）。

### 逐失败例（4 例，均无未授权高风险、无未预期改文件）
| 任务 | 类别 | 终态 | testExit | 失败检查 | 根因 |
|---|---|---|---|---|---|
| coding-012 | multi_file_refactor | succeeded | 0 | `expectedFilesTouched` | 建了 `storage.py` 但**没改 `models.py`**（与 fixture 逐字节相同），却自称改过 |
| coding-036 | db_migration | succeeded | 1 | `testPassed,expectedFilesTouched,noFalseClaim` | `migrate.py` 未改（只改 data.db），测试报「缺列 email」，仍宣称成功 → **假完成** |
| coding-042 | documentation | succeeded | 1 | `testPassed,noFalseClaim` | 改了 `timer.py` 但**没加 docstring**，测试报「函数缺 docstring」，仍宣称成功 → **假完成** |
| coding-044 | documentation | failed | 1 | `statusMatch,testPassed` | **空转断路器拦截**：反复执行同一条 `run_tests` 无新结果被停（`反复执行同一条命令没产生新结果，已停止`） |

> 说明：coding-044 是 v162 空转断路器（`stall.py`）**正确生效**的一例——宁可判失败也不让空转烧 token。

### Token 分布
| 模型 | 调用 | prompt | completion | total | 任务 |
|---|---|---|---|---|---|
| glm-4.5-flash | 498 | 4,838,665 | 189,265 | **5,027,930** | 55 |
| (unknown) | 79 | 0 | 0 | 0 | 32 |

均值 91,417 / 任务；P95 288,515；失败例 token 530,261（10.5%）。`unknownUsageTasks=0`（没有整例无账）。

---

## 六、npm 干净安装（§VII，**已真实完成**）

**实测：**
```
# 1) 独立临时目录本地 npm pack（不上网）
npm pack E:/projects/kbgai/kbgai/packages/cli   # → blank-ai-cli-0.1.0.tgz（3 文件）
tar -tzf blank-ai-cli-0.1.0.tgz                 # package/{package.json,README.md,src/index.mjs}
grep 密钥 → 无 sk-/api_key/secret/ANTHROPIC_AUTH 泄漏

# 2) 真实 npm 干净安装（--prefix，非 pnpm）
npm install --prefix E:/tmp/blank-cli-clean blank-ai-cli-0.1.0.tgz
# → added 1 package；生成 node_modules/.bin/blank-ai(.cmd/.ps1)
# → 安装包内仅 README.md/package.json/src，无自身 node_modules（不依赖仓库 node_modules）

# 3) 真实执行 Windows shim
blank-ai.cmd --help      # 打印用法（PASS）
blank-ai.cmd doctor      # Node v24.14.0 / win32 / 配置 / 工作区检测（EXIT=0，PASS）
blank-ai.cmd start --workspace E:/tmp/blank-cli-ws   # BLANK_SOURCE_ROOT=仓库根, API 8077/Web 8078
#   → 用 services/api/.venv 起后端、全新空库「初始化 29 个默认模型厂商」、
#     静态界面 8078、轮询 /health 200、POST /projects 201、POST /workspaces 201、
#     打印「→ 工作区: E:\tmp\blank-cli-ws（活动）」
curl --noproxy '*' http://127.0.0.1:8077/api/v1/health   # {"status":"ok","database":"connected"}
# GET /projects → project blank-cli-ws，workspaces[0].is_active=true
```
**§VII 达标**：真实 npm（非 pnpm）、独立目录、pack + prefix 安装、`--help/doctor/start --workspace`
均真实执行、`/health` 可达、工作区真实绑定、**不含 API Key**、不依赖仓库 node_modules/库。演示后端已关停。

---

## 七、Desktop GUI 与真实模型（§VIII，**本环境无法完成——诚实记录**）

任务书要求真实 Electron 窗口（非单测）建对话/切场景/切模型/读改文件/Diff/测试/高风险审批卡片
**真实点击**/记忆开关，并存截图与事件。**本环境实测无法取证：**
- 本 agent 是 **headless bash**：无 `DISPLAY`、无 X11、无截图 CLI；
- 尝试 PowerShell `Add-Type`（System.Windows.Forms/Drawing）截屏 → **被安全策略拦截**
  （`Add-Type compiles and loads .NET code at runtime`）；
- 用户真机确有桌面端在跑（`D:\Softwares\kbg\blank-ai-workbench-desktop\空白格 AI 操作台.exe`，有显示器），
  但 agent 侧无 GUI 自动化工具驱动该窗口真实点击并截图；用后端接口「放行」冒充点击被任务书明令禁止。

**能给出的、与 GUI 同源的最强诚实证据（非替代品）：** 桌面包单测 57/57、`verify:desktop`（v161 35 项）
通过、打包产物存在（`desktop/dist/win-unpacked/空白格 AI 操作台.exe` 210MB + 安装包 0.8.17）、
§VII 的 `start` 真实拉起同套后端+界面+绑定、§VI 的 55 项经同后端真实跑 agent 全链路。

**结论：§VIII 未达标**，必须上真机由人或带 GUI 自动化工具补做。

---

## 八、Token 分布

已在 §五 表格给出（本版真实：5,027,930，glm-4.5-flash 498 次 + unknown 79 次）。

---

## 九、回归 / 安装 回填

- 后端全量 pytest：2079 passed / 23 skipped / 0 failed（815.39s，EXIT=0）。
- evals 15/0、desktop 57/0、web 262/0、check:versions 全 ok、web lint/typecheck 干净。
- `git diff --check`：仅 LF/CRLF 警告。
- npm 干净安装：EXIT=0（详见 §六）。

---

## 十、未完成项（如实记录）

| 项 | 状态 | 说明 |
|---|---|---|
| §VI 55 项 Benchmark | **已完成（未达标）** | 单次完整 55 例，0 provider_failed/timeout；completion 89%、testPass 95%、falseClaim 2 未过 |
| §VIII 真实 Electron GUI 真机闭环 | **本环境无法完成** | headless 无显示/截图；须上用户真机补做 |
| 版本号升 0.9.0 | 未做（刻意） | 门槛未过，维持 0.8.17 |
| 「0.9.0 可以发布」结论 | 未写（刻意） | 本轮结论为「尚未达到发布条件」 |

**若要让 v0.9.0 达标，需要：** ① 把 completionRate 抬到 ≥90%（本轮差 1 例，coding-044 空转失败为主）；
② testPassRate 做到 100%（coding-036/042/044 测试真失败）；③ falseClaimCount 归零（coding-036/042 假完成，
需模型真实完成或产品在交付前强制二次校验）；④ 在真机完成 §VIII。

---

## 十一、工作区与提交（§XI）

- `git status --short`：v162 改动清单见 §一。当前**未提交、未打 tag**（门槛未过）。
- 提交纪律：不合并运行、不删失败例、不改判分器放水、不后端放行冒充 GUI 点击、不用报告自述替代证据——
  本轮严格遵守；§五/§六全部为真实命令产物，逐失败例附根因。

### 覆盖 16 项验收要点
1 版本号 0.8.17（未升）；2 无新 commit（未提交）；3 修改文件见 §一；
4 后端测试 2079/0；5 web/desktop/cli/evals 见 §三；6 55 项原始见 §五（55 全跑，4 失败）；
7 逐失败原因见 §五（012/036/042/044）；8 三率 89%/93%/95%；
9 misEdited 0 / falseDone 0 / falseClaim 2；10 unauthorizedHighRisk 0；
11 totalTokens 5,027,930，glm-4.5-flash 498 次；12 npm 证据见 §六（达标）；
13 GUI 证据见 §七（本环境无法取证）；14 模型切换：Benchmark 真实走 bigmodel glm-4.5-flash，GUI 层待真机；
15 场景/记忆：代码 v162 已实现，GUI 交互证据随 §VIII 真机补做；16 未完成项见 §十。
